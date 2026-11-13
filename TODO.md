# TODO

All items done. Remaining notes:

- FOSSology `/report` endpoints (readmeoss/SPDX) were evaluated and not adopted: reports are
  generated asynchronously per upload (trigger, poll, download), which is no cheaper than the
  upload-level `/licenses/scanned` and `/copyrights` summaries now used.
- The FOSSology UI link (`?mod=license&upload=ID&item=TOP`) is unverified against a real server.
