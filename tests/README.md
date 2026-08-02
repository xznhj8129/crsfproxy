# Tests

Run the repository test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

`test_crsf_rx_sim.py` exercises the virtual receiver's UDP RC packet validation, CRSF link-statistics frame layout, source timeout behavior, simulated RF-loss behavior, and runtime control commands.
