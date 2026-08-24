# Zotero Literature Curator Skill

An agent skill for turning a real local Zotero collection tree into a curated literature library:

1. connects to Zotero Desktop through its local Connector;
2. inventories the current lowest-level categories;
3. guides an agent to research authoritative or landmark papers that match each category;
4. downloads a local PDF into a matching folder;
5. imports the item and managed PDF attachment into Zotero; and
6. assigns a tag such as `2016-CVPR-ResNet-CCFA` or `2017-PNAS-EWC`;
7. validates that a multi-venue manifest follows an explicit allowlist and distribution policy.

The skill is designed for requests in Chinese or English, including requests such as “给每个 Zotero
最底层分类添加一篇经典论文并下载 PDF” and “populate each leaf collection with a seminal CCF-A
paper.”

## Why The Connector

The implementation talks to the Zotero Desktop Connector at `http://127.0.0.1:23119`. It uses the
application-owned save flow:

```text
saveItems -> updateSession (collection + tag) -> saveAttachment (local PDF)
```

It does **not** directly access `zotero.sqlite`. That keeps collection changes and managed
attachments under Zotero's own process.

## Requirements

- Zotero Desktop running locally, with an editable library and file storage.
- Python 3.10+; the scripts use only the standard library.
- Network access for source verification and open PDF downloads.
- A reviewed literature manifest. The agent should verify venue, paper identity, and source URL
  before downloading, rather than trusting a search-result title alone.

Check the current installation first:

```bash
python scripts/zotero_curator.py status
```

A healthy local instance reports Connector API version, Zotero version, editable status, collection
count, and leaf count.

## Install As A Local Skill

Clone the repository and copy it into Codex's user skill directory:

```bash
git clone https://github.com/lzeeorno/zotero-literature-curator-skill.git /tmp/zotero-literature-curator-skill
mkdir -p ~/.codex/skills
cp -a /tmp/zotero-literature-curator-skill ~/.codex/skills/zotero-literature-curator
```

The installed entry point is:

```text
~/.codex/skills/zotero-literature-curator/SKILL.md
```

For local development, the repository directory itself is a complete skill package. It contains
`SKILL.md`, runnable scripts, a manifest template, tests, and research-facing documentation.

After installation, point commands at the installed script, even when the run workspace is outside
the repository:

```bash
CURATOR="${CODEX_HOME:-$HOME/.codex}/skills/zotero-literature-curator/scripts/zotero_curator.py"
```

When developing directly in a clone, run the equivalent `python scripts/zotero_curator.py ...`
commands from that clone instead.

## Quick Start

Use a workspace outside the repository for manifests, PDFs, and state:

```bash
mkdir -p work
python "$CURATOR" status
python "$CURATOR" inventory --leaves-only --out work/zotero-leaves.tsv
cp "${CURATOR%/scripts/zotero_curator.py}/templates/manifest.tsv" work/manifest.tsv
```

Have the agent fill one paper row per target leaf. The required fields are shown below; the full
field contract is in [references/manifest.md](references/manifest.md).

| Field | Example | Purpose |
| --- | --- | --- |
| `collection_id` | `C123` | Live Zotero leaf collection ID |
| `collection_name` | `监督/半监督分割` | Makes the retained PDF path readable |
| `title` | `Deep Residual Learning for Image Recognition` | Verified bibliographic title |
| `year` | `2016` | Tag year |
| `venue` | `CVPR` | Tag venue abbreviation |
| `short_tag` | `ResNet` | Concise method or contribution name |
| `ccf` | `CCFA` or blank | Optional verified CCF suffix |
| `pdf_spec` | `arxiv:1512.03385` | Open PDF source |
| `source_url` | `https://arxiv.org/abs/1512.03385` | Canonical paper page |

For multi-venue work, pass `--allowed-venues`, `--required-venues`, `--min-per-venue`,
`--max-per-venue`, and/or `--max-venue-share` to `validate` and `import`. These checks prevent a
manifest from silently becoming a single-source batch. For example, a non-CVPR batch can require
all of `ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,
Science Robotics,MIA` and cap any one venue at 25%.

Then validate, download, inspect, and import:

```bash
python "$CURATOR" validate --manifest work/manifest.tsv --check-targets --require-all-leaves \
  --allowed-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --required-venues "ICML,NeurIPS,ICLR,AAAI,ICCV,MICCAI,TPAMI,TMI,Lancet,Nature,Science,Nature Communications,Science Robotics,MIA" \
  --min-per-venue 1 --max-venue-share 0.25

python "$CURATOR" download \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs

python "$CURATOR" import \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs \
  --state work/import-state.json \
  --dry-run \
  --require-all-leaves

python "$CURATOR" import \
  --manifest work/manifest.tsv \
  --pdf-root work/Zotero_PDFs \
  --state work/import-state.json \
  --require-all-leaves
```

The retained PDF layout is deterministic:

```text
work/Zotero_PDFs/
  C123_监督-半监督分割/
    2016-ResNet.pdf
```

The final command creates Zotero parent items, applies tags in the format
`year-venue-short_tag[-ccf]`, and attaches the already-downloaded local PDFs as managed Zotero
attachments. Its state file resumes known-safe intermediate phases without re-importing an existing
parent item. If a request outcome is genuinely uncertain, it reports the exact collection for
inspection instead of creating a duplicate. Connector sessions live in the running Zotero Desktop
process, so restart recovery for an incomplete row requires manual reconciliation before rerunning.

## Literature Curation Rules

The agent's research stage should use the live collection names and user constraints to choose
papers. For a “seminal” request, the expected choices are original or field-defining papers rather
than generic recent surveys. Good sources include CCF-A venues where appropriate and authoritative
journals or proceedings such as `Nature`, `Science`, `The Lancet`, TPAMI, MIA, CVPR, ICCV, ICML,
NeurIPS, ICLR, and AAAI.

Use a primary metadata source plus a legal, direct PDF source. Prefer arXiv, PMC, official open
proceedings, publisher open access, or an author-hosted paper. Do not guess the CCF level: write
`CCFA`, `CCFB`, or `CCFC` only when the venue's affiliation has been checked. Authoritative sources
without an applicable CCF label keep a three-part tag, for example `2017-PNAS-EWC`.

## Commands

| Command | Effect |
| --- | --- |
| `status` | Read-only Connector health and editable-library check |
| `inventory --leaves-only` | Exports the current leaf collections as TSV |
| `validate` | Checks manifest fields, IDs, tag construction, and optionally live Zotero targets |
| `download` | Fetches and validates local PDF copies into category folders |
| `import --dry-run` | Checks the prepared import without changing Zotero |
| `import` | Creates Zotero parent items, tags, and managed PDF attachments |

Use `--endpoint http://127.0.0.1:23119` before the subcommand when a nondefault local Connector
endpoint is required.

Use `--require-all-leaves` with `validate --check-targets` and `import` for the full-library
workflow. It rejects a parent category, a stale collection ID, or a manifest that leaves any current
lowest-level Zotero category uncovered. Omit it only when the user explicitly requests a selected
subset of leaf collections.

## Connector Unavailable

If Zotero Desktop or its Connector is unavailable, the skill preserves the category-resolved PDF
tree and manifest for manual import. It does not fall back to SQLite writes and does not claim that
Zotero has been updated until the Connector import has actually returned success.

## Verification

The test suite runs against a mock Connector and a mock PDF server. It validates the leaf-detection
logic, tag formatting, PDF download check, and the exact Connector import sequence without changing
your Zotero library:

```bash
python -m unittest discover -s tests -v
```

## Repository Hygiene

The `.gitignore` excludes downloaded PDFs, user manifests, local import state, Zotero databases,
and transient Python files. The public repository contains only reusable code, template data, and
documentation.

## License

[MIT](LICENSE)
