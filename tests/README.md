# Tests

Run the repository test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

`test_envelope.py` exercises the protocol v1 header, session/version/kind validation, sequence wrap comparison, and STATUS packing.

`test_server.py` exercises the CONTROL high-water/reply cache (including A/B/A replay and eviction), the RC owner/stale/foreign/release rules, and the CONTROL worker error mapping.

`test_config.py` exercises the ExpressLRS service's summary/paginated results, compact versus full parameter objects, and cache invalidation.

`test_crsf_rx_sim.py` exercises the virtual receiver's protocol v1 RC frame validation, JSON CONTROL commands, CRSF link-statistics frame layout, source timeout behavior, simulated RF-loss behavior, and local UDP envelope round trips.

`test_config_client.py` exercises the CONTROL client against a local fake UDP server: success replies, busy retries, error codes, and paginated `params` collection.
