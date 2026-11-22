# Clean-room spec: operator and exfiltration identifiers in kit code and page scripts

## Source studied
- URL: https://github.com/cybercdh/kitphishr. Last commit 2026-09-29 (SHA [U]). Licence GPL-2.0 with project clarifications, dual-licensed commercially (`LICENSE`, `LICENSING.md`).
- Files read: `internal/analyze/analyze.go`, `mail.go`, `authors.go`, `backdoor.go` (skim), `brands.go`, `transcode.go` (skim), `internal/hunt/targets.go`, `scan.go`, `opendir.go` (skim). Studied 2026-11-21.
- Complementary permissive source: Lookyloo/har2tree `helper.py` (identifier extraction, BSD-3).

## Purpose
Extract identifiers that tie a page or kit to a single operator account. These are where stolen data or leads go (Telegram bots, Discord webhooks, mail drops), and which account owns anti-bot widgets (captcha sitekeys). They also include kit author credits, which identify a kit family rather than an operator. In HYDRA they become operator-tier or kit-tier genes.

## Inputs
Text bodies already collected by HYDRA: HTML, inline and external JS, PHP or other source files when a kit archive or saved directory is part of the incident, POST bodies and request URLs from the HAR. Each body comes with its path or URL.

## Behaviour
1. Skip bodies whose path is inside a bundled-library location (vendor, node_modules, mailer libraries, test-framework folders) or matches the library allowlist. This avoids library author emails and credits.
2. Telegram bot token: a decimal account number of 8-12 digits, a colon, then a 30-45 character token from the URL-safe alphabet, on word boundaries. Also match the same token inside `api.telegram.org/bot<token>/` URLs. Store only a salted hash (via `pii_hash`) plus the numeric bot id, which is not secret.
3. Telegram chat id: a `chat_id` key followed by `=` or `:`, an optional quote, and a signed 6-13 digit number. Also match it as a query parameter in a Telegram API URL. Negative ids (groups/channels) are kept with their sign.
4. Discord webhook: an http(s) URL on discord.com or discordapp.com (optionally with the ptb or canary subdomain) with path api, optional version, webhooks, numeric id, token. Store the numeric webhook id, and the token only as a hash.
5. Mail drops (kit source only): find calls to mail-sending functions and mailer recipient setters. Take the recipient argument. If it is a string literal containing an email, take it. If it is a variable, search the same file for simple assignments of a string literal to that variable and take emails from those. Drop placeholder domains (example.*, your-domain, localhost, test@test-like pairs). Keep free-mail providers.
6. Captcha sitekeys: elements whose class is a known widget class (reCAPTCHA, hCaptcha, Cloudflare Turnstile) and carry a site-key data attribute. Also capture the `sitekey`/`render=` parameter of the captcha API script URL. Drop the vendor-published public test keys (keep a short warninglist).
7. Kit author credits: phrases such as "coded / made / developed / created by" followed by a 3-30 character handle, author tags in comments, and ICQ (6-10 digits) or Skype handles near their labels. Drop common words and generic tool names (false-positive list).
8. Output each value with its source path or URL and byte offset for the explanation.

## Parameters and thresholds
- Scan cap: 3 MB per body.
- Tiers and starting quality (to be calibrated):

  | Value | Tier | Quality |
  |---|---|---|
  | bot token hash | operator | 1.0 |
  | chat id | operator | 1.0 |
  | webhook id | operator | 1.0 |
  | mail drop | operator, contact group | 0.8 |
  | captcha sitekey | operator | 0.9 |
  | author credit | kit | 0.5 |

- Dependency groups: bot token and chat id share `inf:operator:exfil_telegram`. Webhook gets `inf:operator:exfil_discord`. Sitekey gets `inf:operator:captcha`. Author gets `inf:kit:author`.

## Outputs
Gene records (kind, value or hash, tier, group, quality, evidence pointer). There is no network activity.

## Edge cases
- Tokens split across string concatenations: optionally join adjacent literals in JS before scanning (cheap heuristic). Otherwise missed.
- Base64-encoded or obfuscated tokens: decode obvious base64 literals of 40-200 chars once and rescan. Never execute code.
- The same sitekey appearing as a public demo key: warninglisted, 0 bits.
- Minified bundles containing many numeric "chat_id"-like keys from SDKs: require the Telegram API host or a bot token in the same body to accept a chat id from JS.

## What NOT to replicate
- No active hunting: no archive probing, no open-directory walking, no downloading kits from live hosts.
- No backdoor-detection rules.
- Do not reuse kitphishr's regex literals, names or structure. Write patterns from the descriptions above.
- Do not store raw bot or webhook tokens. Redact them in exports (`redact.py`).

## Suggested acceptance tests
1. A synthetic JS body with a fake bot token and chat id produces two operator genes. The raw token does not appear in the gene store or exports.
2. The same token inside `/vendor/` produces nothing.
3. A mail drop resolved through one variable assignment is found. `admin@example.com` is not.
4. Two incidents with different HTML but the same Turnstile sitekey link with operator bits. The vendor test sitekey gives 0 bits.
5. "coded by" inside jQuery's licence header is ignored (library allowlist).

## Similarity-check reminder
Before merging, compare the implementation with kitphishr's Go sources (token/regex overlap check). Ensure no copied patterns, identifiers or comments. Patterns must be derived from public format documentation (Telegram Bot API, Discord webhook URL format, captcha vendor docs).

## Similarity-check result (2026-11-21, builder sub-agent of OPERATOR-GENES)
- Implementation: `hydra/genome/kit_exfil.py` (extractor `inf_kit@1`), token patterns in `hydra/redact.py`, tests
  `tests/genes_inf/test_kit_exfil.py`. Written from this spec and public format documentation (Telegram Bot API,
  Discord webhook URL format, reCAPTCHA / hCaptcha / Turnstile docs); the builder did not open kitphishr.
- Reference: cybercdh/kitphishr @ fd23be9772483dea64a60d4bb03b080e9201dec1 (shallow clone on the server in
  `/tmp/opgenes_simcheck/repos`, never read; 44 text files, 35 817 tokens). Method:
  `python3 -I scripts/simcheck_cleanroom.py <our files> -- <repo>` (aggregate numbers only).

| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams | Shared 12+ char strings |
|---|---|---|---|---|---|
| hydra/genome/kit_exfil.py | 3715 | 4 | 42 | 0 | 0 |
| hydra/redact.py | 971 | 4 | 42 | 0 | 0 |
| tests/genes_inf/test_kit_exfil.py | 1678 | 5 | 17 | 0 | 0 |
| hydra/genome/tollfraud.py (own code after the CERT Polska post, checked as well) | 4809 | 5 | 41 | 0 | 0 |

The normalised run of 42 is the generic shape of consecutive compiled-pattern assignments (`ID = ID.ID(STR)`); the
pre-existing `hydra/redact.py`, written before this spec, shows the same 42, and no string literal (regex) of 12+
characters is shared. Conclusion: no long identical token sequences, no copied patterns, identifiers or comments.
