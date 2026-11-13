# TODO

1. **Mark optional fields on the Add connection page.** If a token env is given, the user env,
   password env and fixture path are not needed; label them "(optional)" and say so in the form.
2. **Searchable folder picker in New Mapping.** Let the user paste or type a full (or partial)
   folder name and filter the SL scan/folder list as they type, instead of relying on the
   select's type-ahead on the first few letters.
3. **Edit and delete connections.** Add UI and API to edit a connection's name, base URL and
   auth env names, and to delete it (with a guard or confirmation when mappings still use it).
