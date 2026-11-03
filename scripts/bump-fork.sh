#!/bin/sh
# Выпустить и закрепить форк мода одной командой:
#   scripts/bump-fork.sh superbwarfare        # тег bfN+1 на main форка → сборка → релиз → пин здесь
#   scripts/bump-fork.sh superbwarfare bf7    # закрепить уже существующий релиз bf7
# Форки: create, parcool, superbwarfare, simplebedrockmodel. Нужен gh с правом push в форк.
# Тег ставится на ветку форка по умолчанию: main.
# jar в git не хранится: метафайл ссылается на GitHub Release форка, сборка пака качает его сама.
set -eu
mod="${1:?использование: scripts/bump-fork.sh <create|parcool|superbwarfare|simplebedrockmodel> [bfN]}"
cd "$(dirname "$0")/.."
branch=main
case "$mod" in
    create) repo=Blockfield/create-fabric ;;
    parcool) repo=Blockfield/parcool-fabric ;;
    superbwarfare) repo=Blockfield/superbwarfare-fabric ;;
    simplebedrockmodel) repo=Blockfield/simplebedrockmodel-fabric ;;
    *)
        echo "неизвестный форк: $mod (create | parcool | superbwarfare | simplebedrockmodel)"
        exit 1
        ;;
esac
meta="mods/$mod.pw.toml"
# Первый пин: метафайла ещё нет, старого jar тоже.
old=""
[ -f "$meta" ] && old=$(sed -n 's/^filename = "\(.*\)"$/\1/p' "$meta")
[ -f "$meta" ] && title=$(sed -n '1p' "$meta")

tag="${2:-}"
if [ -z "$tag" ]; then
    n=$(printf '%s' "$old" | sed -n 's/.*-bf\([0-9][0-9]*\)\.jar$/\1/p')
    tag="bf$((${n:-0} + 1))"
    while gh api "repos/$repo/git/ref/tags/$tag" >/dev/null 2>&1; do tag="bf$((${tag#bf} + 1))"; done
    sha=$(gh api "repos/$repo/commits/$branch" -q .sha)
    gh api "repos/$repo/git/refs" -f ref="refs/tags/$tag" -f sha="$sha" >/dev/null
    echo "$repo: тег $tag на $branch ($sha), жду сборку…"
    run=""
    for _ in $(seq 30); do
        run=$(gh run list --repo "$repo" --branch "$tag" --limit 1 --json databaseId -q '.[0].databaseId // empty')
        [ -n "$run" ] && break
        sleep 5
    done
    [ -n "$run" ] || {
        echo "сборка для $tag не запустилась: https://github.com/$repo/actions"
        exit 1
    }
    gh run watch "$run" --repo "$repo" --exit-status >/dev/null ||
        {
            echo "сборка упала: https://github.com/$repo/actions/runs/$run"
            exit 1
        }
fi

# Рядом с jar в релизе может лежать jar исходников (AGPL): он не мод.
name=$(gh release view "$tag" --repo "$repo" --json assets -q '[.assets[].name | select(endswith(".jar") and (endswith("-sources.jar") | not))][0] // empty')
[ -n "$name" ] || {
    echo "в релизе $tag ($repo) нет jar"
    exit 1
}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
gh release download "$tag" --repo "$repo" -p "$name" -D "$tmp"
printf '%s\nfilename = "%s"\nside = "both"\n\n[download]\nurl = "https://github.com/%s/releases/download/%s/%s"\nhash-format = "sha512"\nhash = "%s"\n' \
    "$title" "$name" "$repo" "$tag" "$name" "$(sha512sum "$tmp/$name" | cut -d' ' -f1)" >"$meta"
if [ -n "$old" ] && [ "$old" != "$name" ]; then
    git rm -q --cached --ignore-unmatch "files/mods/$old"
    rm -f "files/mods/$old"
fi
./.tools/packwiz refresh >/dev/null
echo "готово: $mod → $tag ($name). Дальше: бамп version в pack.toml, коммит, тег пака."
