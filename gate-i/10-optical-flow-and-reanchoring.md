# Optical Flow And Reanchoring

Tracking still uses the existing optical-flow implementation.

Gate I adds runtime-mode limits for tracking points and transitions target-loss states through `target_lost` and `recovering` so detection can re-anchor without uncontrolled loops.

