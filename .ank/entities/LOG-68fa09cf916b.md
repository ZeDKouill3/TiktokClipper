---
id: LOG-68fa09cf916b
type: log
title: "Correctif applique : I1 _stable_face_over_null renvoie (None, stables) des que len(periods) <= 1"
created: 2026-10-10T21:11:13Z
author: w-cee612eefa16
scope:
  - clipper/reframe.py
  - tests/test_reframe.py
about: TASK-cee612eefa16
seq: 4
schema: 4
version: 1
---

 (_persistent -> False sur periode unique) ; M2 _size_camera_rect h = _even(w / aspect), controle extrait en _check_camera_rect_ratio avec tolerance max(0.02, 2/w + 2/h) ; M5 shutil.rmtree(video_dir/facecam) avant les planches. Mesures apres : scan bh 16..400 -> 0 refuse ; 112x78 accepte par _reframe_stream ; 540x480 toujours refuse ; period_7.jpg supprime. 6 tests rouges -> verts. tests/test_reframe.py : 181 verts, 5 echecs OOM OpenCV sous xdist (Insufficient memory) rejoues en serie : 6 verts.
