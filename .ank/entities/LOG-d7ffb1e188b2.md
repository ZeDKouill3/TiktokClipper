---
id: LOG-d7ffb1e188b2
type: log
title: "ank done #1 : verifier tests FAILED (578 s, pytest exit 3, -n 3, 2,9 Go libres). lastfailed = 3"
created: 2026-10-10T19:36:27Z
author: w-5b0bef2e6edd
scope:
  - clipper/parts.py
  - tests/test_parts.py
about: TASK-5b0bef2e6edd
seq: 4
schema: 4
version: 1
---

 tests test_reframe (facecam_detection_is_cached_per_video, clip_with_a_live_but_faceless_facecam_stays_stream, clip_with_a_black_facecam_stays_letterbox_with_a_logged_reason), hors scope parts. Rejoues cibles : 2 verts, 1 rouge avec cv2.error OpenCV alloc.cpp:73 '(-4:Insufficient memory) Failed to allocate 6220800 bytes' dans reframe.py:2216 (image_reader). Cause : RAM du PC epuisee (autres sessions), pas le correctif. Relance ank done quand la memoire libre remonte.
