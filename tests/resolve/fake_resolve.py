"""A fake of Resolve's documented scripting API that holds a real model: matchcut's model of the documentation, NOT evidence about Resolve.

What it models: a project manager with folders and projects (one current project), a project's media pool, timelines with video and audio
items that have record and source frames, transitions between items, markers with customData, a playhead, and ``Timeline.Export``
written from the model (OTIO by hand, without matchcut's metadata; FCPXML 1.10 and EDL with matchcut's own writers from the model).
``ImportTimelineFromFile`` reads the file with matchcut's own readers: the fake imports exactly what matchcut's readers read.

The allow-list: ``DOCUMENTED`` is every method of every class in the Resolve 21.0.4 README (community copy,
https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8), parsed from its full text into ``readme_21_0_4.json``, independently of
the probe script's own list.  ``GATED`` are calls reported for 21.1+ and absent from that
README; the fake answers them only when its install's catalogue documents them (``Variant.catalogue``).  Every other public attribute is
rejected (AttributeError) and logged in ``World.rejected``, so a test can assert that the script never tried one: the script catches
exceptions, so a rejection would otherwise only show up as a recorded failure.

The semantics chosen here (``endFrame`` exclusive, ``recordFrame`` absolute, marker frame ids relative to the timeline start, default
start timecode 01:00:00:00) are matchcut's reading of the README; the probe exists to measure the real ones.
"""
from __future__ import annotations

import json
import os
import re
import types
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from matchcut.interchange.common import SourceInfo, assemble, layout, parse_timecode
from matchcut.interchange.edl import _tc, export_edl, import_edl
from matchcut.interchange.fcpxml import export_fcpxml, import_fcpxml
from matchcut.interchange.otio import import_otio

from . import readme_calls

# -- the allow-list ----------------------------------------------------------------------------------------------------------------
# every method of each class in the Resolve 21.0.4 README's API reference, parsed from its full text (readme_calls.py): the source of record
DOCUMENTED: Dict[str, set] = {"DaVinciResolveScript": {"scriptapp"}}
DOCUMENTED.update({cls: set(names) for cls, names in readme_calls.load()["classes"].items()})
# reported for 21.1+ (third-party), not in the 21.0.4 README: answered only when the fake install's catalogue documents them
GATED: Dict[str, set] = {"TimelineItem": {"AddTransition", "SetFades", "GetFades", "GetType"}}
EXPORT_CONSTANTS = ("EXPORT_AAF", "EXPORT_DRT", "EXPORT_EDL", "EXPORT_FCP_7_XML", "EXPORT_FCPXML_1_8", "EXPORT_FCPXML_1_9", "EXPORT_FCPXML_1_10",
                    "EXPORT_HDR_10_PROFILE_A", "EXPORT_HDR_10_PROFILE_B", "EXPORT_TEXT_CSV", "EXPORT_TEXT_TAB", "EXPORT_DOLBY_VISION_VER_2_9",
                    "EXPORT_DOLBY_VISION_VER_4_0", "EXPORT_DOLBY_VISION_VER_5_1", "EXPORT_OTIO", "EXPORT_ALE", "EXPORT_ALE_CDL", "EXPORT_NONE",
                    "EXPORT_AAF_NEW", "EXPORT_AAF_EXISTING", "EXPORT_CDL", "EXPORT_SDL", "EXPORT_MISSING_CLIPS")
PAGES = {"media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
MARKER_COLORS = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}
CLIP_INFO_KEYS = {"mediaPoolItem", "startFrame", "endFrame", "mediaType", "trackIndex", "recordFrame"}
IMPORT_OPTIONS = {"timelineName", "importSourceClips", "sourceClipsPath", "sourceClipsFolders", "interlaceProcessing"}
PRODUCT = "matchcut fake of the documented Resolve API"
ADD_TRANSITION_DOC = '''    def AddTransition(self, options: TransitionOptions) -> TimelineItem:
        """(fake catalogue text, written by matchcut's tests) Adds a transition at the end of this item.
        options: {"type": "Cross Dissolve", "alignment": "center", "duration": <frames>}"""'''


@dataclass
class Variant:
    """What the fake install does.  Default: keeps everything, Studio-like 21.1 install whose catalogue documents AddTransition."""

    name: str = "keeps everything"
    connected: bool = True  # scriptapp("Resolve") returns None when False
    current_project: str = "Untitled Project"  # the project open when the probe starts
    catalogue: bool = True  # a catalogue (README.md + .pyi) is installed
    documents_add_transition: bool = True  # the catalogue mentions AddTransition (and GetType)
    lose_dissolves: bool = False  # imports turn dissolves into cuts; AddTransition returns None
    shift: int = 0  # source frames read back (and exported) shifted by this many frames
    drop_markers: bool = False  # imports carry no markers; AddMarker returns True but keeps nothing
    append_none: bool = False  # AppendToTimeline returns [None] per clip and adds nothing
    import_media_dupes: bool = False  # ImportMedia of a file already in the pool imports it again (default: returns [])
    start_tc: str = "01:00:00:00"  # the start timecode of every new timeline
    raise_on: Tuple[str, ...] = ()  # "Class.Method" names that raise a RuntimeError
    user_timelines: int = 0  # what the open project already holds (an Untitled Project with work in it)
    user_clips: int = 0
    user_subfolders: int = 0  # empty bins under the root
    user_bins: Tuple[Any, ...] = ()  # a bin tree under the root: ((name, clips, (children...)), ...)
    root_folder_none: bool = False  # MediaPool.GetRootFolder returns None
    create_does_not_switch: bool = False  # CreateProject makes the project but leaves the open one open
    switch_away_on: str = ""  # "Class.Method": when the probe calls it, the open project becomes the user's again
    switch_on_call: int = 1  # ... on that call's n-th use (1: the first)
    switch_to: str = ""  # ... or this project (by name) instead of the user's
    unreadable_bin: str = ""  # "clips" or "folders": the first bin's GetClipList / GetSubFolderList returns None
    src_end_inclusive: bool = False  # GetSourceEndFrame reads the last source frame (one less than matchcut's exclusive end)
    end_frame_inclusive: bool = False  # AppendToTimeline takes endFrame as the last frame (the built clip is one frame longer)
    transition_items: bool = False  # GetItemListInTrack lists transitions as items whose GetType() is "transition"
    transition_at_cut: bool = False  # ... and puts each one starting at the cut instead of centred on it
    marker_info_plain: bool = False  # GetMarkerByCustomData returns the marker's information without its frame
    version: Tuple[Any, ...] = (21, 1, 0, 0, "")


VARIANTS = {
    "keeps everything": Variant(),
    "loses dissolves": Variant(name="loses dissolves", lose_dissolves=True),
    "shifts frames by 1": Variant(name="shifts frames by 1", shift=1),
    "drops markers": Variant(name="drops markers", drop_markers=True),
    "AddTransition not documented": Variant(name="AddTransition not documented", documents_add_transition=False),
    "AppendToTimeline returns [None]": Variant(name="AppendToTimeline returns [None]", append_none=True),
    "scripting not connected": Variant(name="scripting not connected", connected=False),
    "a user project is open": Variant(name="a user project is open", current_project="Client job v3", user_timelines=2, user_clips=3),
    "untitled with a timeline": Variant(name="untitled with a timeline", user_timelines=1),
    "untitled with a pool clip": Variant(name="untitled with a pool clip", user_clips=1),
    "untitled with a pool folder": Variant(name="untitled with a pool folder", user_subfolders=1),
    "an empty named project": Variant(name="an empty named project", current_project="Nouveau projet"),
    "empty nested bins": Variant(name="empty nested bins", current_project="Untitled Project",
                                 user_bins=(("Bin 1", 0, (("Inner", 0, ()),)), ("Bin 2", 0, ()))),
    "a clip in a nested bin": Variant(name="a clip in a nested bin", user_bins=(("Bin 1", 0, (("Inner", 1, ()),)),)),
    "bins past the depth cap": Variant(name="bins past the depth cap", user_bins=(("A", 0, (("B", 0, (("C", 0, (("D", 0, ()),)),)),)),)),
    "bins past the count cap": Variant(name="bins past the count cap", user_bins=tuple((f"Bin {k}", 0, ()) for k in range(60))),
    "a bin whose clips cannot be read": Variant(name="a bin whose clips cannot be read", user_subfolders=1, unreadable_bin="clips"),
    "a bin whose folders cannot be read": Variant(name="a bin whose folders cannot be read", user_subfolders=1, unreadable_bin="folders"),
    "the open project changes on the last export": Variant(name="the open project changes on the last export", switch_away_on="Timeline.Export",
                                                           switch_on_call=15),
    "the open project changes before CreateProject": Variant(name="the open project changes before CreateProject",
                                                             switch_away_on="ProjectManager.GotoRootFolder", switch_to="Client job v2"),
    "timeline count unreadable": Variant(name="timeline count unreadable", raise_on=("Project.GetTimelineCount",)),
    "root folder unreadable": Variant(name="root folder unreadable", root_folder_none=True),
    "CreateProject does not switch": Variant(name="CreateProject does not switch", create_does_not_switch=True),
    "the open project changes mid-run": Variant(name="the open project changes mid-run", switch_away_on="MediaPool.CreateEmptyTimeline"),
    "GetSourceEndFrame inclusive": Variant(name="GetSourceEndFrame inclusive", src_end_inclusive=True),
    "endFrame inclusive": Variant(name="endFrame inclusive", end_frame_inclusive=True),
    "transition items": Variant(name="transition items", transition_items=True),
    "transition items at the cut": Variant(name="transition items at the cut", transition_items=True, transition_at_cut=True),
    "marker info without frame": Variant(name="marker info without frame", marker_info_plain=True),
    "Resolve 18.5": Variant(name="Resolve 18.5", version=(18, 5, 0, 0, ""), raise_on=("TimelineItem.GetSourceStartFrame", "TimelineItem.GetSourceEndFrame")),
}


# -- the model --------------------------------------------------------------------------------------------------------------------
@dataclass
class Media:
    path: str
    name: str
    src: SourceInfo


@dataclass
class Item:
    media: Media
    start: int  # absolute record frame
    dur: int
    src_in: int  # source start frame (from the media's frame 0)
    track: str = "video"


@dataclass
class TimelineModel:
    name: str
    start: int
    start_tc: str
    rate: Fraction
    video: List[Item] = field(default_factory=list)
    audio: List[Item] = field(default_factory=list)
    transitions: Dict[int, Tuple[int, int]] = field(default_factory=dict)  # incoming video item index -> (frames, head before the cut)
    markers: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # frame from the timeline start -> marker
    playhead: int = 0

    @property
    def end(self) -> int:
        return max([self.start] + [i.start + i.dur for i in self.video + self.audio])


@dataclass
class Bin:
    name: str
    clips: List[Media] = field(default_factory=list)
    children: List["Bin"] = field(default_factory=list)


def _bins(spec: Tuple[Any, ...], src: Optional[SourceInfo]) -> List[Bin]:
    return [Bin(name, [Media(src.path, Path(src.path).name, src) for _ in range(n)] if src else [], _bins(kids, src)) for name, n, kids in spec]


@dataclass
class ProjectModel:
    name: str
    folder: Tuple[str, ...]
    settings: Dict[str, str] = field(default_factory=lambda: {"timelineFrameRate": "24"})
    pool: List[Media] = field(default_factory=list)
    timelines: List[TimelineModel] = field(default_factory=list)
    current_timeline: Optional[TimelineModel] = None
    bins: List["Bin"] = field(default_factory=list)  # media pool folders under the root
    touched: bool = False  # anything changed by the API


class World:
    """The fake install: projects in folders, the current project, the call log and what was rejected."""

    def __init__(self, variant: Variant, src: SourceInfo):
        self.variant, self.src = variant, src
        self.folders: Dict[Tuple[str, ...], set] = {(): {"Client work"}, ("Client work",): set()}
        self.projects: Dict[Tuple[str, ...], Dict[str, ProjectModel]] = {(): {}, ("Client work",): {}}
        self.cwd: Tuple[str, ...] = ()
        user = ProjectModel(variant.current_project, ())
        user.timelines = [TimelineModel(f"Timeline {k + 1}", 86400, "01:00:00:00", Fraction(24)) for k in range(variant.user_timelines)]
        if src is not None:
            user.pool = [Media(src.path, Path(src.path).name, src) for _ in range(variant.user_clips)]
        user.bins = [Bin(f"Bin {k + 1}") for k in range(variant.user_subfolders)] + _bins(variant.user_bins, src)
        self.user = user
        self.projects[()][user.name] = user
        other = ProjectModel("Client job v2", ("Client work",))
        self.projects[("Client work",)][other.name] = other
        self.current = user
        self.log: List[Tuple[Any, ...]] = []
        self.trace: List[str] = []  # every documented method the probe looked up, in order (it calls what it looks up)
        self.rejected: List[str] = []
        self.saved: List[str] = []
        self.page = "edit"

    # the API a test sees
    def allowed(self, cls: str, name: str) -> bool:
        if name in DOCUMENTED.get(cls, set()):
            return True
        return name in GATED.get(cls, set()) and self.variant.catalogue and self.variant.documents_add_transition

    def all_projects(self) -> List[ProjectModel]:
        return [p for by_name in self.projects.values() for p in by_name.values()]

    def calls(self, name: str) -> List[Tuple[Any, ...]]:
        return [c for c in self.log if c[0] == name]

    def write_catalogue(self, folder: Path) -> Path:
        """The installed catalogue the probe looks for in RESOLVE_SCRIPT_API (a README.md and a .pyi, as 21.1 ships them)."""
        folder.mkdir(parents=True, exist_ok=True)
        if not self.variant.catalogue:
            return folder
        lines = ["# DaVinci Resolve scripting (fake catalogue written by matchcut's tests)", "", "Last Updated: fake 21.1.0", ""]
        lines += [f"{cls}.{m}()" for cls, names in sorted(DOCUMENTED.items()) for m in sorted(names)]
        (folder / "README.md").write_text("\n".join(lines) + "\n")
        pyi = ["# fake DaVinciResolveScript.pyi", "class TimelineItem:"]
        if self.variant.documents_add_transition:
            pyi += [ADD_TRANSITION_DOC, "    def GetType(self) -> str: ...", "    def SetFades(self, fades: dict) -> bool: ..."]
        else:
            pyi += ["    def GetName(self) -> str: ..."]
        (folder / "DaVinciResolveScript.pyi").write_text("\n".join(pyi) + "\n")
        return folder


class _Api:
    """Base of every fake API object: public attributes outside the allow-list are rejected and logged."""

    _cls = ""

    def __init__(self, world: World):
        object.__setattr__(self, "_world", world)

    def __getattribute__(self, name: str) -> Any:
        if name[:1].isupper() and not name.startswith("EXPORT_"):
            world = object.__getattribute__(self, "_world")
            cls = object.__getattribute__(self, "_cls")
            if not world.allowed(cls, name):
                world.rejected.append(f"{cls}.{name}")
                raise AttributeError(f"{cls}.{name} is not in the documented API of this install")
            try:
                fn = object.__getattribute__(self, name)
            except AttributeError:
                world.rejected.append(f"{cls}.{name} (documented, not modelled by the fake)")
                raise
            world.trace.append(f"{cls}.{name}")
            if f"{cls}.{name}" == world.variant.switch_away_on and world.trace.count(f"{cls}.{name}") == world.variant.switch_on_call:
                # (someone opens another project while the probe runs)
                world.current = next((p for p in world.all_projects() if p.name == world.variant.switch_to), world.user)
            if f"{cls}.{name}" in world.variant.raise_on:
                def boom(*a: Any, **k: Any) -> Any:
                    world.log.append((f"{cls}.{name}",) + a)
                    raise RuntimeError(f"fake failure in {cls}.{name}")
                return boom
            return fn
        return object.__getattribute__(self, name)

    def _log(self, name: str, *args: Any) -> None:
        self._world.log.append((f"{self._cls}.{name}",) + args)


def _frames(tc: str, rate: Fraction) -> Optional[int]:
    parsed = parse_timecode(tc, rate)
    return parsed[0] if parsed else None


class FakeTimelineItem(_Api):
    _cls = "TimelineItem"

    def __init__(self, world: World, tl: TimelineModel, item: Item):
        super().__init__(world)
        self.tl, self.item = tl, item

    def _shift(self) -> int:
        return self._world.variant.shift

    def GetName(self):
        return self.item.media.name

    def GetStart(self, subframe_precision=False):
        return self.item.start

    def GetEnd(self, subframe_precision=False):
        return self.item.start + self.item.dur

    def GetDuration(self, subframe_precision=False):
        return self.item.dur

    def GetLeftOffset(self, subframe_precision=False):
        return self.item.src_in + self._shift()

    def GetRightOffset(self, subframe_precision=False):
        return self.item.media.src.duration_frames - (self.item.src_in + self._shift() + self.item.dur)

    def GetSourceStartFrame(self):
        return self.item.src_in + self._shift()

    def GetSourceEndFrame(self):
        return self.item.src_in + self._shift() + self.item.dur - (1 if self._world.variant.src_end_inclusive else 0)

    def GetMediaPoolItem(self):
        return FakeMediaPoolItem(self._world, self.item.media)

    def GetType(self):
        return {"video": "video", "audio": "audio", "transition": "transition"}[self.item.track]

    def AddTransition(self, options):
        self._log("AddTransition", dict(options))
        if not isinstance(options, dict) or self._world.variant.lose_dissolves or "duration" not in options:
            return None
        idx = self.tl.video.index(self.item) + 1  # at the end of this item: the transition into the next one
        if idx >= len(self.tl.video):
            return None
        frames = int(options["duration"])
        self.tl.transitions[idx] = (frames, frames // 2)
        return True

    def SetFades(self, fades):
        raise AssertionError("the probe must not call SetFades")


class FakeTimeline(_Api):
    _cls = "Timeline"

    def __init__(self, world: World, project: ProjectModel, tl: TimelineModel):
        super().__init__(world)
        self.project, self.tl = project, tl

    def __eq__(self, other):
        return isinstance(other, FakeTimeline) and other.tl is self.tl

    def __hash__(self):
        return id(self.tl)

    def GetName(self):
        return self.tl.name

    def GetStartFrame(self):
        return self.tl.start

    def GetEndFrame(self):
        return self.tl.end

    def GetStartTimecode(self):
        return self.tl.start_tc

    def GetSetting(self, settingName=None):
        return self.project.settings.get(settingName, "") if settingName else dict(self.project.settings)

    def GetTrackCount(self, trackType):
        assert trackType in ("audio", "video", "subtitle"), trackType
        return {"video": 1, "audio": 1 if self.tl.audio or self._world.src.has_audio else 0, "subtitle": 0}[trackType]

    def GetItemListInTrack(self, trackType, index):
        assert trackType in ("audio", "video", "subtitle") and isinstance(index, int) and index >= 1
        items = {"video": self.tl.video, "audio": self.tl.audio}.get(trackType, []) if index == 1 else []
        out = []
        for k, i in enumerate(items):
            if trackType == "video" and self._world.variant.transition_items and k in self.tl.transitions:
                frames, head = self.tl.transitions[k]
                at = i.start if self._world.variant.transition_at_cut else i.start - head
                out.append(FakeTimelineItem(self._world, self.tl, Item(i.media, at, frames, 0, "transition")))
            out.append(FakeTimelineItem(self._world, self.tl, i))
        return out

    def AddMarker(self, frameId, color, name, note, duration, customData):
        assert color in MARKER_COLORS and isinstance(duration, int) and isinstance(customData, str), (color, duration, customData)
        self._log("AddMarker", frameId, color, name, note, duration, customData)
        self.project.touched = True
        if self._world.variant.drop_markers:
            return True  # says yes, keeps nothing
        if frameId in self.tl.markers:
            return False
        self.tl.markers[int(frameId)] = {"color": color, "duration": float(duration), "note": note, "name": name, "customData": customData}
        return True

    def GetMarkers(self):
        return {float(f): dict(m) for f, m in sorted(self.tl.markers.items())}

    def GetMarkerByCustomData(self, customData):
        for f, m in sorted(self.tl.markers.items()):
            if m["customData"] == customData:
                return dict(m) if self._world.variant.marker_info_plain else {float(f): dict(m)}
        return {}

    def UpdateMarkerCustomData(self, frameId, customData):
        self._log("UpdateMarkerCustomData", frameId, customData)
        if self._world.variant.drop_markers or int(frameId) not in self.tl.markers:
            return False
        self.tl.markers[int(frameId)]["customData"] = customData
        return True

    def SetCurrentTimecode(self, timecode):
        self._log("SetCurrentTimecode", timecode)
        f = _frames(timecode, self.tl.rate)
        if f is None or not self.tl.start <= f < self.tl.end:
            return False
        self.tl.playhead = f
        return True

    def GetCurrentTimecode(self):
        parsed = parse_timecode(self.tl.start_tc, self.tl.rate)
        return _tc(self.tl.playhead, self.tl.rate, bool(parsed and parsed[1]))

    def GetCurrentVideoItem(self):
        for i in self.tl.video:
            if i.start <= self.tl.playhead < i.start + i.dur:
                return FakeTimelineItem(self._world, self.tl, i)
        return None

    def Export(self, fileName, exportType, exportSubtype):
        self._log("Export", fileName, exportType, exportSubtype)
        consts = _constants()
        if exportType == consts["EXPORT_EDL"] and exportSubtype not in (consts["EXPORT_NONE"], consts["EXPORT_CDL"], consts["EXPORT_SDL"], consts["EXPORT_MISSING_CLIPS"]):
            return False
        if exportType == consts["EXPORT_OTIO"]:
            text = json.dumps(_otio_of(self.tl, self._world), indent=1)
        elif exportType == consts["EXPORT_FCPXML_1_10"]:  # matchcut's writer, then without matchcut's metadata, as an editor writes it
            text = re.sub(r"\s*<metadata>.*?</metadata>|\s*<keyword [^>]*/>", "",
                          export_fcpxml([(self.tl.name, _timeline_of(self.tl, self._world), [])], self._world.src, name=self.tl.name), flags=re.S)
        elif exportType == consts["EXPORT_EDL"]:
            text = "".join(ln for ln in export_edl(_timeline_of(self.tl, self._world), self.tl.name, self._world.src, self.tl.name).splitlines(True)
                           if not ln.startswith("* MATCHCUT"))
        else:
            return False
        Path(fileName).write_text(text, encoding="utf-8")
        return True


class FakeMediaPoolItem(_Api):
    _cls = "MediaPoolItem"

    def __init__(self, world: World, media: Media):
        super().__init__(world)
        self.media = media

    def GetName(self):
        return self.media.name

    def GetClipProperty(self, propertyName=None):
        props = {"Clip Name": self.media.name, "File Path": self.media.path, "Frames": str(self.media.src.duration_frames)}
        return props if propertyName is None else props.get(propertyName, "")


class FakeFolder(_Api):
    _cls = "Folder"

    def __init__(self, world: World, project: ProjectModel):
        super().__init__(world)
        self.project = project

    def GetName(self):
        return "Master"

    def GetClipList(self):
        return [FakeMediaPoolItem(self._world, m) for m in self.project.pool]

    def GetSubFolderList(self):
        return [FakeSubFolder(self._world, self.project, b) for b in self.project.bins]


class FakeSubFolder(FakeFolder):
    def __init__(self, world: World, project: ProjectModel, bin_: Bin):
        super().__init__(world, project)
        self.bin = bin_

    def GetName(self):
        return self.bin.name

    def _unreadable(self, what: str) -> bool:
        return self._world.variant.unreadable_bin == what and self.project.bins[:1] == [self.bin]

    def GetClipList(self):
        return None if self._unreadable("clips") else [FakeMediaPoolItem(self._world, m) for m in self.bin.clips]

    def GetSubFolderList(self):
        return None if self._unreadable("folders") else [FakeSubFolder(self._world, self.project, b) for b in self.bin.children]


class FakeMediaPool(_Api):
    _cls = "MediaPool"

    def __init__(self, world: World, project: ProjectModel):
        super().__init__(world)
        self.project = project

    def GetRootFolder(self):
        return None if self._world.variant.root_folder_none else FakeFolder(self._world, self.project)

    def _media(self, path: str) -> Optional[Media]:
        return next((m for m in self.project.pool if m.path == path), None)

    def _add(self, path: str) -> Media:
        m = Media(path, Path(path).name, self._world.src)
        self.project.pool.append(m)
        self.project.touched = True
        return m

    def ImportMedia(self, items):
        self._log("ImportMedia", list(items))
        out = []
        for p in items:
            assert isinstance(p, str), p
            if not os.path.isfile(p):
                continue
            if self._media(p) is not None and not self._world.variant.import_media_dupes:
                continue
            out.append(FakeMediaPoolItem(self._world, self._add(p)))
        return out

    def _new_timeline(self, name: str) -> Optional[TimelineModel]:
        if any(t.name == name for t in self.project.timelines):
            return None
        rate = _rate_of(self.project.settings.get("timelineFrameRate", "24"))
        start = _frames(self._world.variant.start_tc, rate) or 0
        tl = TimelineModel(name, start, self._world.variant.start_tc, rate, playhead=start)
        self.project.timelines.append(tl)
        self.project.current_timeline = tl
        self.project.touched = True
        return tl

    def CreateEmptyTimeline(self, name):
        self._log("CreateEmptyTimeline", name)
        tl = self._new_timeline(name)
        return FakeTimeline(self._world, self.project, tl) if tl else None

    def AppendToTimeline(self, clipInfos):
        self._log("AppendToTimeline", [dict(c) for c in clipInfos])
        tl = self.project.current_timeline
        assert tl is not None
        out = []
        for info in clipInfos:
            assert set(info) <= CLIP_INFO_KEYS, set(info) - CLIP_INFO_KEYS
            if self._world.variant.append_none:
                out.append(None)
                continue
            media = info["mediaPoolItem"].media
            dur = int(info["endFrame"]) - int(info["startFrame"]) + (1 if self._world.variant.end_frame_inclusive else 0)
            rec = int(info.get("recordFrame", tl.end))
            item = Item(media, rec, dur, int(info["startFrame"]))
            tl.video.append(item)
            if media.src.has_audio and info.get("mediaType") in (None, 2):
                tl.audio.append(Item(media, rec, dur, int(info["startFrame"]), "audio"))
            out.append(FakeTimelineItem(self._world, tl, item))
        return out

    def ImportTimelineFromFile(self, filePath, importOptions=None):
        options = dict(importOptions or {})
        assert set(options) <= IMPORT_OPTIONS, set(options) - IMPORT_OPTIONS
        self._log("ImportTimelineFromFile", filePath, options)
        src = self._world.src
        text = Path(filePath).read_text(encoding="utf-8")
        ext = Path(filePath).suffix.lower()
        markers: List[Tuple[int, Dict[str, Any]]] = []
        if ext == ".otio":
            doc = json.loads(text)
            cut, _ = import_otio(doc, None, src)
            for m in doc["tracks"]["children"][0].get("markers", []):
                meta = (m.get("metadata") or {}).get("matchcut") or {}
                markers.append((int(m["marked_range"]["start_time"]["value"]),
                                {"color": "Green", "duration": 1.0, "note": m.get("comment", ""), "name": m.get("name", ""), "customData": str(meta.get("junction_id", ""))}))
        elif ext == ".fcpxml":
            cut, _ = import_fcpxml(text, None, None, src)  # the fake carries no FCPXML markers across
        elif ext == ".edl":
            if self._media(src.path) is None:  # the fake's EDL import links only to a clip already in the pool
                return None
            cut, _ = import_edl(text, src.rate, src.path, None, src)
        else:
            return None
        tl = self._new_timeline(options.get("timelineName") or Path(filePath).stem)
        if tl is None:
            return None
        media = self._media(src.path) or (self._add(src.path) if options.get("importSourceClips", True) else None)
        if media is None:
            return None
        for k, row in enumerate(layout(cut, src.rate)):
            tl.video.append(Item(media, tl.start + row.cut, row.dur, row.vis_in))
            if src.has_audio:
                tl.audio.append(Item(media, tl.start + row.cut, row.dur, row.vis_in, "audio"))
            if row.tr and not self._world.variant.lose_dissolves:
                tl.transitions[k] = (row.tr, row.head)
        if not self._world.variant.drop_markers:
            tl.markers.update(dict(markers))
        return FakeTimeline(self._world, self.project, tl)


class FakeProject(_Api):
    _cls = "Project"

    def __init__(self, world: World, model: ProjectModel):
        super().__init__(world)
        self.model = model

    def GetName(self):
        return self.model.name

    def GetMediaPool(self):
        return FakeMediaPool(self._world, self.model)

    def GetSetting(self, settingName=None):
        return self.model.settings.get(settingName, "") if settingName else dict(self.model.settings)

    def SetSetting(self, settingName, settingValue):
        self._log("SetSetting", settingName, settingValue)
        if self.model.timelines:  # (the fake's rule) the frame rate is fixed once a timeline exists
            return False
        self.model.settings[settingName] = str(settingValue)
        self.model.touched = True
        return True

    def SetCurrentTimeline(self, timeline):
        if not isinstance(timeline, FakeTimeline) or timeline.tl not in self.model.timelines:
            return False
        self.model.current_timeline = timeline.tl
        return True

    def GetTimelineCount(self):
        return len(self.model.timelines)

    def GetTimelineByIndex(self, idx):
        return FakeTimeline(self._world, self.model, self.model.timelines[idx - 1])

    def GetCurrentTimeline(self):
        tl = self.model.current_timeline
        return FakeTimeline(self._world, self.model, tl) if tl else None


class FakeProjectManager(_Api):
    _cls = "ProjectManager"

    def GetCurrentProject(self):
        return FakeProject(self._world, self._world.current) if self._world.current else None

    def GetCurrentDatabase(self):
        return {"DbType": "Disk", "DbName": "Local Database"}

    def GotoRootFolder(self):
        self._log("GotoRootFolder")
        self._world.cwd = ()
        return True

    def GetFolderListInCurrentFolder(self):
        return sorted(self._world.folders[self._world.cwd])

    def CreateFolder(self, folderName):
        self._log("CreateFolder", folderName)
        here = self._world.folders[self._world.cwd]
        if folderName in here:
            return False
        here.add(folderName)
        self._world.folders[self._world.cwd + (folderName,)] = set()
        self._world.projects[self._world.cwd + (folderName,)] = {}
        return True

    def OpenFolder(self, folderName):
        self._log("OpenFolder", folderName)
        if folderName not in self._world.folders[self._world.cwd]:
            return False
        self._world.cwd = self._world.cwd + (folderName,)
        return True

    def GetCurrentFolder(self):
        return self._world.cwd[-1] if self._world.cwd else ""

    def GetProjectListInCurrentFolder(self):
        return sorted(self._world.projects[self._world.cwd])

    def CreateProject(self, projectName, mediaLocationPath=None):
        self._log("CreateProject", projectName)
        here = self._world.projects[self._world.cwd]
        if projectName in here:
            return None
        p = ProjectModel(projectName, self._world.cwd)
        here[projectName] = p
        if not self._world.variant.create_does_not_switch:
            self._world.current = p  # CreateProject switches to the new project
        return FakeProject(self._world, p)

    def SaveProject(self):
        self._log("SaveProject", self._world.current.name)
        self._world.saved.append(self._world.current.name)
        return True


class FakeResolve(_Api):
    _cls = "Resolve"

    def __init__(self, world: World):
        super().__init__(world)
        for k, v in _constants().items():
            object.__setattr__(self, k, v)

    def GetProjectManager(self):
        return FakeProjectManager(self._world)

    def GetProductName(self):
        return PRODUCT

    def GetVersion(self):
        return list(self._world.variant.version)

    def GetVersionString(self):
        return ".".join(str(x) for x in self._world.variant.version[:4]) + " (fake)"

    def OpenPage(self, pageName):
        self._log("OpenPage", pageName)
        if pageName not in PAGES:
            return False
        self._world.page = pageName
        return True


def _constants() -> Dict[str, int]:
    return {name: k + 1 for k, name in enumerate(EXPORT_CONSTANTS)}


def _rate_of(text: str) -> Fraction:
    return {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "59.94": Fraction(60000, 1001)}.get(str(text), Fraction(str(text)))


def _otio_of(tl: TimelineModel, world: World) -> Dict[str, Any]:
    """OTIO from the model, as an editor writes it: no matchcut metadata, the media's start timecode on the source side."""
    r = float(tl.rate)
    src = world.src

    def rt(v: float) -> Dict[str, Any]:
        return {"OTIO_SCHEMA": "RationalTime.1", "rate": r, "value": float(v)}

    def rng(a: float, d: float) -> Dict[str, Any]:
        return {"OTIO_SCHEMA": "TimeRange.1", "start_time": rt(a), "duration": rt(d)}

    def clip(item: Item) -> Dict[str, Any]:
        s = item.src_in + world.variant.shift
        ref = {"OTIO_SCHEMA": "ExternalReference.1", "name": item.media.name, "target_url": Path(item.media.path).as_uri(),
               "available_range": rng(src.start_tc_frames, src.duration_frames), "metadata": {}}
        return {"OTIO_SCHEMA": "Clip.2", "name": item.media.name, "metadata": {"Resolve_OTIO": {}}, "source_range": rng(src.start_tc_frames + s, item.dur),
                "effects": [], "markers": [], "enabled": True, "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"}

    def track(kind: str, items: List[Item], with_tr: bool, markers: List[Dict[str, Any]]) -> Dict[str, Any]:
        kids: List[Dict[str, Any]] = []
        for k, item in enumerate(items):
            if with_tr and k in tl.transitions:
                frames, head = tl.transitions[k]
                kids.append({"OTIO_SCHEMA": "Transition.1", "name": "Cross Dissolve", "metadata": {}, "transition_type": "SMPTE_Dissolve",
                             "in_offset": rt(head), "out_offset": rt(frames - head)})
            kids.append(clip(item))
        return {"OTIO_SCHEMA": "Track.1", "name": kind[0] + "1", "kind": kind, "children": kids, "markers": markers, "effects": [], "metadata": {},
                "source_range": None, "enabled": True}

    markers = [{"OTIO_SCHEMA": "Marker.2", "name": m["name"], "comment": m["note"], "color": m["color"].upper(), "marked_range": rng(f, 1),
                "metadata": {"Resolve_OTIO": {"Keywords": [], "Note": m["note"]}, "customData": m["customData"]}} for f, m in sorted(tl.markers.items())]
    tracks = [track("Video", tl.video, True, markers)] + ([track("Audio", tl.audio, False, [])] if tl.audio else [])
    return {"OTIO_SCHEMA": "Timeline.1", "name": tl.name, "global_start_time": rt(tl.start), "metadata": {"Resolve_OTIO": {}},
            "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "", "children": tracks, "markers": [], "effects": [], "metadata": {}, "source_range": None,
                       "enabled": True}}


def _timeline_of(tl: TimelineModel, world: World):
    """A matchcut Timeline of the model's video track (for the FCPXML and EDL writers)."""
    items: List[Tuple[str, Dict[str, Any]]] = []
    for k, item in enumerate(tl.video):
        if k in tl.transitions:
            frames, head = tl.transitions[k]
            items.append(("tr", {"in": head, "out": frames - head, "kind": "dissolve"}))
        items.append(("clip", {"vis_in": item.src_in + world.variant.shift, "dur": item.dur, "name": item.media.name, "meta": {}}))
    return assemble(items, tl.rate, {"stage": tl.name}, tl.name, world.src.path, None)


def module(world: World) -> types.ModuleType:
    """A stand-in for ``DaVinciResolveScript``: ``scriptapp("Resolve")`` returns the fake (None when the install is not connected)."""
    mod = types.ModuleType("DaVinciResolveScript")
    mod.__file__ = "/fake/Developer/Scripting/Modules/DaVinciResolveScript.py"

    def scriptapp(name):
        world.log.append(("DaVinciResolveScript.scriptapp", name))
        return FakeResolve(world) if world.variant.connected and name == "Resolve" else None

    mod.scriptapp = scriptapp
    return mod
