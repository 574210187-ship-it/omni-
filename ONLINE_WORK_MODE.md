# One online execution line

> **Integration contract only.** This public Phase-1 repository does not
> implement the Work Mode, Feishu connector, record lock, file bridge, upload,
> or remote readback described below. It documents the safety requirements for
> a future separately implemented integration; local testing must follow
> `WORKBUDDY_TEST.md` instead.

Work Mode selects and locks one eligible Feishu record, materializes the source
through the approved native file bridge, validates it locally, invokes
ClipFlow Visual v0.6, and only after a PASS appends the three artifacts and
performs a remote readback. A HOLD ends that row before encoding or upload.

The media package itself uses video pixels as its sole text evidence. It does
not contain or call an audio-to-text route.
