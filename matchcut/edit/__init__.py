"""The edit core: frame-native, inspectable edits of a matchcut cut on its canonical timeline.

``model`` holds the context every operation and rule reads (``EditContext``) and ``Refused``; ``ops`` the nine operations; ``rules`` the
proposal-wide checks, the timing report, the change report and ``describe``; ``session`` the store ``WS/edits/`` (revisions, proposals,
apply, undo/redo/checkout, compare, staleness and the lock); ``view`` inspect, the frames at a junction, previews and renders, and an editor
pin's transition protection (``pin_transition``); ``tools`` the tool table every transport serves, and ``__main__`` the JSON CLI
(``python -m matchcut.edit WS COMMAND '{json}'``).  The pipeline engine never imports this package.
"""
