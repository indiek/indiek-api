#!/usr/bin/env bash
# Rebuild the CLI-made demo archive used by the tests (tests/fixtures/demo_archive).
#
#   cd fastAPI && tests/fixtures/build_demo_archive.sh <new-empty-dir>
#
# Every item is written by the `ik` CLI with a throwaway author key that only
# lives in this process.  Resulting aliases: e1–e10 (e5 uri "PhD thesis", e6
# local_file, e1 node and e8 edge superseded), m1 node tag "theorem", m2 edge
# tag "proof" (m1 on e1, m2 on e8).  The e6 local_file node records the
# absolute path of demo_sources/atiffimage.tiff on the building machine.
set -euo pipefail

dest="${1:?usage: $0 <new-empty-dir>}"
if [[ -e "$dest" && -n "$(ls -A "$dest")" ]]; then
    echo "refusing to write into non-empty $dest" >&2
    exit 1
fi
mkdir -p "$dest"
sources="$(cd "$(dirname "$0")/demo_sources" && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

export IK_ARCHIVE_PATH="$(cd "$dest" && pwd)"
export IK_CACHE_DIR="$scratch/cache"
ik() { uv run ik --dot-env "$scratch/none.env" "$@" >/dev/null; }
IK_AUTHOR_PRIVATE_KEY="$(uv run ik --dot-env "$scratch/none.env" keygen 2>/dev/null | cut -d= -f2)"
export IK_AUTHOR_PRIVATE_KEY

ik create_node --content "Gödel's incompleteness theorem"                 # e1
ik create_node --content "$sources/statement.txt"                         # e2
ik create_node --content "$sources/Proof.md"                              # e3
ik create_node --content "$sources/diagram.png"                           # e4
ik create_node --resource-type uri --title "PhD thesis" \
    --uri https://adrianradillo.org/media/pdfs/phd_dissertation_radillo_2018.pdf \
    --media-type "application/pdf; profile=pdfa"                         # e5
ik create_node --resource-type local_file --path "$sources/atiffimage.tiff"  # e6
ik create_edge --content states --source-node e2 --target-node e1        # e7
ik create_edge --content proves --source-node e3 --target-node e2        # e8
ik create_node_tag --name theorem --description "A proven statement"     # m1
ik create_edge_tag --name proof --description "Proof relation"           # m2
ik set_tag --tag m1 --id e1                                              # m3, m4
ik set_tag --tag m2 --id e8                                              # m5, m6
ik supersede_node --node-id e1 --content "Gödel's *first* incompleteness theorem"
ik supersede_edge --edge-id e8 --content "sketches a proof of"
ik create_node --content "$sources/andtext.txt"                           # e9
ik create_edge --content cites --source-node e9 --target-node e1         # e10
ik manifest
echo "demo archive written to $IK_ARCHIVE_PATH" >&2
