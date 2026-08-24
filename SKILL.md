---
name: zotero-literature-curator
description: Curate a local Zotero library from a nested collection architecture. Use this skill whenever a user asks to inspect local Zotero collections, fill every lowest-level category with landmark or authoritative literature, search and download local paper PDFs, import those PDFs into Zotero, or apply tags in the format YYYY-VENUE-ShortMethod[-CCFLevel]. It supports Chinese and English research-library requests and uses Zotero Desktop's local Connector rather than direct database edits.
---

# Zotero Literature Curator

Use this skill for a complete, evidence-backed workflow from a local Zotero hierarchy to
collection-specific papers, locally retained PDFs, Zotero items, and consistent tags.

## Scope And Boundaries

- Start from Zotero Desktop's local Connector at `http://127.0.0.1:23119`.
- Use `scripts/zotero_curator.py` for Connector calls. It invokes Zotero's `saveItems`,
  `updateSession`, and `saveAttachment` endpoints and never opens or writes `zotero.sqlite`.
- Treat the current Connector target tree as authoritative. Do not reuse collection IDs from an
  earlier library or assume a textual hierarchy has already been created locally.
- Keep every source PDF in a readable local directory as well as attaching it as a managed Zotero
  file. Do not commit downloaded papers, manifests containing a user's reading list, or import
  state files to a public repository.

## Workflow

### 1. Connect And Inventory

Zotero Desktop must be running with an editable library selected. Set the installed command path and
check the live connection first:

```bash
CURATOR="${CODEX_HOME:-$HOME/.codex}/skills/zotero-literature-curator/scripts/zotero_curator.py"
python "$CURATOR" status
python "$CURATOR" inventory --leaves-only --out work/zotero-leaves.tsv
```

The `inventory` command reads the Connector's `targets` response and derives leaves from Zotero's
actual depth levels. The output becomes the category source of truth for this run.

If the Connector is unavailable, do not touch Zotero's database. Create the corresponding
`Zotero_PDFs/<collection-id>_<collection-name>/` directory structure and retain the validated PDFs
there. State plainly that files are ready for manual Zotero attachment, rather than claiming import
success.

### 2. Research Papers Before Downloading

For each lowest-level collection, create one reviewed manifest row. First build a compact research
matrix with the collection, candidate paper, year, venue, landmark contribution, CCF affiliation if
verified, canonical landing page, and openly obtainable PDF source.

When the user requests classic or seminal work, prefer the original paper that introduced the
method or established the benchmark. Honor the explicit venue allowlist and do not silently substitute a nearby venue. For a multi-venue request, build a source-distribution matrix before downloading: every required venue must appear, and no single venue may dominate. For the non-CVPR venue set (`ICML`, `NeurIPS`, `ICLR`, `AAAI`, `ICCV`, `MICCAI`, `TPAMI`, `TMI`, `Lancet`, `Nature`, `Science`, `Nature Communications`, `Science Robotics`, `MIA`), use these checks unless the user specifies a different policy:

```text
--allowed-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA"
--required-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA"
--min-per-venue 1 --max-venue-share 0.25
```

For a small manifest where the share cap is too strict, use an explicit integer `--max-per-venue`
and record the reason in the research matrix. Medical leaves should weight `MICCAI`, `TMI`, `MIA`,
and the clinical journals more heavily, while preserving the required multi-venue coverage. A
venue that is authoritative but not explicitly classified by CCF remains valid, but must not
receive a made-up CCF suffix.

Use primary proceedings, publisher, author, arXiv, or PMC pages to verify metadata. Prefer open
PDF sources in this order: arXiv/PMC, official proceedings or publisher open access, then an author
repository. Do not use a landing page, abstract page, or paywalled redirect as a PDF URL.

Copy `"${CURATOR%/scripts/zotero_curator.py}/templates/manifest.tsv"` into the run workspace and fill it. Read
`references/manifest.md` for exact fields. The important values are:

```text
collection_id, collection_name, title, year, venue, short_tag, ccf, pdf_spec, source_url
```

`short_tag` is a concise method name or title summary, for example `ResNet`, `UNet`, `SAM`,
`StableDiffusion`, or `TopoMamba`. For new manifests, `pdf_spec` must be `arxiv:<identifier>` or
`url:<direct-open-pdf-url>`. A legacy `search:<title>` value is accepted only when the reviewed
`source_url` is an arXiv landing page, from which the exact PDF can be derived.

### 3. Validate And Download Local PDFs

Validate the candidate matrix before creating Zotero items:

```bash
python "$CURATOR" validate --manifest work/manifest.tsv --check-targets --require-all-leaves \
  --allowed-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --required-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --min-per-venue 1 --max-venue-share 0.25
python "$CURATOR" download \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs
```

The downloader writes each paper to the matching category folder and verifies that the response is
a nontrivial PDF, rather than HTML or a download-error page. Resolve a bad source before import;
do not attach it as a paper.

### 4. Build Tags And Import Through Zotero

Tags follow exactly:

```text
year-venue-short_tag[-ccf]
```

Examples:

```text
2016-CVPR-ResNet-CCFA
2017-PNAS-EWC
2026-ACMMM-TopoMamba-CCFA
```

The `ccf` column is blank when there is no checked affiliation; otherwise use `CCFA`, `CCFB`, or
`CCFC`. The script normalizes `CCF A` to `CCFA`, but it does not infer affiliations from venue
names.

First run the non-mutating import check, then create the items and attachments:

```bash
python "$CURATOR" import \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs \
  --state work/import-state.json \
  --dry-run --require-all-leaves \
  --allowed-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --required-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --min-per-venue 1 --max-venue-share 0.25

python "$CURATOR" import \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs \
  --state work/import-state.json \
  --require-all-leaves \
  --allowed-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --required-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --min-per-venue 1 --max-venue-share 0.25
```

For every manifest row, the importer creates a Zotero parent item, moves it to the supplied
collection, assigns the computed tag, and attaches the already-downloaded PDF. The state file
records the session phase, so retries resume safe steps without creating a second parent item. If a
request outcome is uncertain, inspect that collection before retrying rather than duplicating it.
Automatic resumption requires the same running Zotero Desktop session, because Connector sessions
are in-memory; after restarting Zotero, reconcile any incomplete row manually before re-running it.

### 5. Report And Audit

At completion, report:

- total current leaf collections and manifest rows;
- every failed source or import, with its exact collection;
- location of the manifest, local PDF tree, and import state;
- the final tag for each imported paper or a compact collection-to-paper table when there are many;
- whether the Connector imported files successfully or a manual fallback remains required.

Do not count folders or arbitrary PDFs as proof of coverage. Verify each manifest row has one
valid PDF path and one imported state record before saying the run is complete.
