# Paper Manifest

Use a tab-separated `manifest.tsv` with one row for every Zotero leaf collection to populate.
The bundled [template](../templates/manifest.tsv) is intentionally fictitious and must be replaced
with reviewed literature candidates.

Required columns:

| Column | Meaning |
| --- | --- |
| `collection_id` | Current Zotero Connector target ID, such as `C123` |
| `collection_name` | Current collection name, retained for a readable PDF folder |
| `title` | Verified paper title |
| `year` | Four-digit publication year |
| `venue` | Conference or journal abbreviation used in the tag |
| `short_tag` | Concise method/model or title summary used in the tag |
| `pdf_spec` | `arxiv:<id>` or `url:<direct-open-pdf-url>` |
| `source_url` | Canonical DOI, publisher, proceedings, or arXiv landing page |

Optional columns:

| Column | Meaning |
| --- | --- |
| `ccf` | Blank, `CCFA`, `CCFB`, or `CCFC`; only add after the venue affiliation is checked |
| `authors` | `Last, First; Last, First` creator list |
| `doi` | Verified DOI, with no fabricated value |
| `abstract` | Brief abstract text when available |
| `item_type` | Zotero item type; defaults to `journalArticle` |
| `language` | Defaults to `en` |

The resulting tag is exactly `year-venue-short_tag[-ccf]`. For example,
`2026-ACMMM-TopoMamba-CCFA` and `2017-PNAS-EWC` are valid outputs.

`pdf_spec` normally accepts only an arXiv identifier or a direct open PDF URL. A legacy
`search:<title>` row is accepted only when its reviewed `source_url` is an arXiv landing page, from
which the exact PDF URL can be derived. A publisher landing page belongs in `source_url`; it must
not be treated as a PDF unless it has been verified to return a real PDF file.
