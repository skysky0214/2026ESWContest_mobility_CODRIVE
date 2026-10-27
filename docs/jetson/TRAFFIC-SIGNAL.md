# Traffic signal update

YOLO traffic-light boxes are classified by existing HSV red/yellow/green masks. Signal selection no longer globally prioritizes red. Eligible box centres: x=0.15..0.85, y<0.70. Score uses horizontal centrality, box area, YOLO confidence and recent location continuity. This is an image-space heuristic, not established association with the vehicle lane. The selected box is highlighted cyan and the published state is shown in the preview.

Three consecutive observations of the same colour and nearby box are required before publishing a known state. Missing candidates, colour changes, new target location or gaps over 300ms reset confirmation. No stale green is retained. Published colour confidence is still the HSV pixel fraction, not a calibrated probability. Directional arrows and flashing signals are not classified.

Validation: 66 tests passed including central green vs side red, confirmation, colour change, missing signal, target change and elapsed-time reset. Indoor camera smoke test verifies the runtime path; actual road signal colour accuracy needs dedicated footage.
