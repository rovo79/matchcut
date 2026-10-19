# Match cutting

![Match cutting](match-cutting.gif)

- Want a high level overview? Read [this blog post](https://netflixtechblog.com/match-cutting-at-netflix-finding-cuts-with-smooth-visual-transitions-31c3fc14ae59?gi=8873f373fd1d)
- Want all the details? Read [the preprint](https://arxiv.org/abs/2210.05766)

### AI editing pipeline

`matchcut.pipeline` is an end-to-end editing pipeline built on the ideas in this repo: it samples the footage, has Claude find the visual narrative and assemble a rough cut, then runs refinement passes (match cuts, continuity, pacing, transitions, model polish) to produce a final cut, an EDL and a render.

```shell
pip install -r requirements-pipeline.txt   # needs ffmpeg on PATH
python -m matchcut.pipeline run footage.mp4 --brief "..." --target-duration 60
python -m matchcut.pipeline run footage.mp4 --backend mock   # offline dry run
python -m matchcut.pipeline run footage.mp4 --music track.mp3 -t 60   # cut to the music
python -m matchcut.tui footage.mp4 --music track.mp3        # control-room TUI (pip install -r requirements-tui.txt)
python -m matchcut.pipeline.cli junctions WORKSPACE   # explain every cut as text
```

See [docs/PIPELINE.md](docs/PIPELINE.md) for the stages, passes, outputs and how to iterate, and [docs/TUI.md](docs/TUI.md) for the terminal control room that drives a run and inspects its shots, story and cut. [docs/M1-ACCEPTANCE.md](docs/M1-ACCEPTANCE.md) is the procedure for judging whether the Edit tab explains cuts well enough without logs. The Edit tab can also play a junction in mpv and flip between the first and the refined cut (`p`, `a`, `s`; `python -m matchcut.review WORKSPACE --index 1 --ab --play`), see [docs/M2B-ACCEPTANCE.md](docs/M2B-ACCEPTANCE.md). A cut can also be exported as OpenTimelineIO, FCPXML and EDL for DaVinci Resolve or Final Cut Pro, and an editor's re-export read back as a `human` stage (`python -m matchcut.pipeline.cli export WORKSPACE`, `... import WORKSPACE FILE`): [docs/INTEROP.md](docs/INTEROP.md) says what each format keeps and loses, and [docs/M2A-ACCEPTANCE.md](docs/M2A-ACCEPTANCE.md) what is verified and what still needs a human with an editor. For DaVinci Resolve Studio, `python -m matchcut.resolve probe WORKSPACE` writes a one-run capability probe that measures which routes to a Resolve timeline work on your install ([docs/M2C-PROBE.md](docs/M2C-PROBE.md); not run against a live Resolve yet, human gate pending).

### Getting started
1. Create and activate the conda environment.
```shell
conda env create -f conda_env.yml
conda activate matchcut
```

2. Download the embeddings

Embeddings are stored as a 3 GB tar file and can be found [here](https://drive.google.com/file/d/1gkXLkvASovS6g_WD5v50NWsjLiw1QP8I/view?usp=sharing). Download this tar file into this directory.

3. Extract
```shell
tar -xf embeddings.tar.gz -C data/
```

At this point you should have all the embeddings placed in the `data/embeddings` folder (expands to 8 GB).

3. Run experiment 1
```shell
python experiment.py --number 1
```

This command will run 195 combinations of encoders, models, and aggregation functions.

4. Run experiment 2
```shell
python experiment.py --number 2
```

This command will run 14 combinations of tasks and encoders.

#### Experiment results caching
By default, experiment results are stored in the `results` folder as pickle files. If you want to re-run experiments you need to remove this folder or to set `cache = False` in `config/common.py`.

#### Required resources and runtimes
Each experiment takes over a day to complete on a machine with 64 CPU cores and 256 GB of memory. If you are running on a GPU machine you will need 32 GB for experiment 2.

Instead, you can pass `--quick` to either experiment and only generate the results for a single combination. For example:
```shell
python experiment.py --number 1 --quick
```

This should take less than a minute to complete for experiment 1 and less than 2 hours for experiment 2.

#### Experiment configs
The following 3 files contain the configs that drive the two experiments:
```
├── matchcut
│   ├── config
│   │   ├── common.py
│   │   ├── experiment1.py
│   │   ├── experiment2.py
```

You can modify these files to run experiments with different configs.

### Data
Take a look at [data-exploration.ipynb](dataset-exploration.ipynb) for example data.

All the data, except for embeddings, is in the `data/` folder:

- `train/validation/test.txt`
  - list of IMDB IDs in each data partition
  - each line contains an IMDB ID
- `dataset-*.json`
  - `dataset-frame.json` and `dataset-motion.json` contain train, validation, and test partitions for the frame and motion tasks.
  - `dataset-random-negatives.json` is the shared set of randomly selected negative pairs.
  - the code in `matchcut/data.py` will take care of combining and filtering these files.
- `shot_indices.json` contains a mapping between IMDB IDs and all the shot indices used in this work.
- `shot-time-ranges.csv` contains a mapping from shot indices to time ranges in seconds. This CSV file has four columns: `imdb_id`, `shot_idx`, `start`, and `end`. `shot_idx` is the shot index that pairs are associated with and `start` and `end` are the beginning and end of the shot relative to the beginning of the movie in seconds.
- `imdb-title-set.csv` IMDB title set with `IMDB ID`, `title`, `genres`, `country`, and `split`.
  
#### Embeddings
See download instructions above.
  - format: `{encoder_name}-{imdb_id}.json`
  - each JSON file is a dictionary with shot indices as keys, and the corresponding embeddings (i.e. a vector of floats) as values.
  - e.g. if file `clip-tt0050706.json` starts with `{"0": [0.1, .2, -0.32, ...]}` then the CLIP embeddings for shot 0 of IMDB ID tt0050706 is `[0.1, .2, -0.32, ...]`

### How do I extend this?
You can extract your own embeddings, store in the format described above, and repeat experiments 1 and 2 by changing the config files in `matchcut/config`.

### Citation
```
@article{chen2022match,
  title={Match Cutting: Finding Cuts with Smooth Visual Transitions},
  author={Chen, Boris and Ziai, Amir and Tucker, Rebecca and Xie, Yuchen},
  journal={arXiv preprint arXiv:2210.05766},
  year={2022}
}
```
