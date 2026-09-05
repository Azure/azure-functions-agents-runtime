# Final application assembly slot

This infrastructure slice intentionally does not define `function_app.py`,
agents, durable routes, or runtime contracts. The final stacked application
layer adds those files beside this marker. The deployment helper refuses to
assemble until `src/function_app.py` exists.
