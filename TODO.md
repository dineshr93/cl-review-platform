# TODO

1. **Mark optional fields on the Add connection page.** If a token env is given, the user env,
   password env and fixture path are not needed; label them "(optional)" and say so in the form.
2. **Searchable folder picker in New Mapping.** Let the user paste or type a full (or partial)
   folder name and filter the SL scan/folder list as they type, instead of relying on the
   select's type-ahead on the first few letters.
3. **Edit and delete connections.** Add UI and API to edit a connection's name, base URL and
   auth env names, and to delete it (with a guard or confirmation when mappings still use it).
4. **Make FOSSology folder refresh fast and non-blocking.** Uploads in a folder are now read
   with 8 parallel workers (`workers` config), but each upload still costs many round trips
   (paged `/licenses`, `/summary`, `/copyrights`, `/topitem`, tree walks, item views), so a
   folder of hundreds of uploads is still slow. Planned, in order of payoff:
   - Run refresh as a background job with progress (uploads done / total) and a cancel button,
     instead of one blocking HTTP request that the browser or proxy can time out.
   - Cache per-upload results (keyed by upload id + upload date) so a re-refresh only reads
     new or changed uploads.
   - Add a "light" refresh mode: skip tree walks, item content views and generic license text
     lookups; fetch the heavy evidence lazily when a component is opened in review.
   - Evaluate FOSSology's report endpoints (`/report`, e.g. readmeoss/SPDX) as one request per
     upload instead of per-file listings.
   - Expose `timeout` and `workers` in the Add/Edit connection form (currently only via config;
     defaults are 180 s and 8).
