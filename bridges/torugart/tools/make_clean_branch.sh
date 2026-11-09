#!/bin/bash
# Ветка для публикации от main одним коммитом. Использование: tools/make_clean_branch.sh [--dry-run] [имя ветки]
# --dry-run: собрать дерево во временный каталог и просканировать на секреты, ничего не создавая.
set -euo pipefail
cd "$(dirname "$0")/../../.."
DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
BR=${1:-torugart-bridge-clean}
TMP=$(mktemp -d)
git ls-files -co --exclude-standard -- bridges/torugart dist .gitignore | while read -r f; do mkdir -p "$TMP/$(dirname "$f")"; cp "$f" "$TMP/$f"; done
python3 bridges/torugart/tools/scan_secrets.py tree "$TMP" --from 20c36b4
if [ $DRY -eq 1 ]; then echo "дерево чистое, файлов: $(find "$TMP" -type f | wc -l), каталог $TMP"; exit 0; fi
git switch -c "$BR" main
git rm -rq --cached . >/dev/null 2>&1 || true
cp -r "$TMP"/. . && git add bridges/torugart dist .gitignore
git commit -q -m "feat(torugart): мост Торугарта: настройки из env, журнал событий, получатели, watchdog, /health"
python3 bridges/torugart/tools/scan_secrets.py history "$BR" --from 20c36b4 && echo "история ветки $BR чистая"
