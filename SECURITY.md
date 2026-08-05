# Security policy

## Reporting

Do not open a public issue containing credentials, session data, private
document links, chat IDs, report content, or personal paths. Report security
concerns privately to the repository owner.

## Secret handling

- Never commit API tokens, cookies, browser profiles, CLI auth files, or
  private keys.
- Keep machine configuration under ignored local paths.
- Treat any credential committed to Git as exposed and rotate it.
- Run a secret scan over the complete proposed history before changing the
  repository to public.

## Content handling

Only process and retain documents the operator is authorized to access.
Source-side download restrictions are terminal states and must not be bypassed.
