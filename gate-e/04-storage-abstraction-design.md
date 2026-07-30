# Storage Abstraction Design

`storage.py` introduces provider-independent storage operations:

- `put_file`
- `get_file`
- `open_file`
- `exists`
- `delete`
- `copy`
- `move`
- `get_size`
- `get_metadata`
- `generate_access_url`
- `list_prefix`

Only `LocalFilesystemStorage` is implemented in Gate E. S3 and other providers remain future work.
