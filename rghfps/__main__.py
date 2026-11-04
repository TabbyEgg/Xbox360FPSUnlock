"""Optional CLI for reproducible preparation and exact-byte patches."""
import argparse
import json
from pathlib import Path
import sys

from .formats import WorkbenchError, bounded_read, inspect_xex
from .catalog import filter_catalog, find_installations, load_catalog
from .patches import bind_signatures, read_recipe, recipe_preview, write_json
from .workflow import export_candidate, extract_disc_input, inspect_game_input, load_project, prepare_game
from .tu import extract_title_update, inspect_title_update


def main():
    parser = argparse.ArgumentParser(prog="python -m rghfps")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read XEX, ISO or GOD metadata without XexTool")
    inspect.add_argument("xex", type=Path)
    prepare = commands.add_parser("prepare", help="Copy, merge the exact TU, decrypt and decompress")
    prepare.add_argument("--game", type=Path, required=True)
    prepare.add_argument("--tu", type=Path)
    prepare.add_argument("--tool", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    scan = commands.add_parser("scan", help="Validate recipe or bind unique signatures to this build")
    scan.add_argument("--project", type=Path, required=True)
    scan.add_argument("--recipe", type=Path, required=True)
    scan.add_argument("--save-recipe", type=Path)
    export = commands.add_parser("export", help="Export a new candidate overlay; never install it")
    export.add_argument("--project", type=Path, required=True)
    export.add_argument("--recipe", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    games = commands.add_parser("games", help="List included recipes, browse references, or scan local games")
    games.add_argument("--query", default="")
    games.add_argument("--references", action="store_true")
    games.add_argument("--folder", type=Path)
    extract = commands.add_parser("extract", help="Extract an Xbox ISO/GOD game without XexTool")
    extract.add_argument("--image", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    tu_info = commands.add_parser("tu-info", help="Inspect a TU package without extracting the game")
    tu_info.add_argument("tu", type=Path)
    tu_extract = commands.add_parser("tu-extract", help="Extract only the small TU package")
    tu_extract.add_argument("tu", type=Path)
    tu_extract.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    log = lambda text: print(text, file=sys.stderr)
    try:
        if args.command == "inspect":
            result = inspect_game_input(args.xex)
        elif args.command == "games":
            catalog = load_catalog(Path(__file__).resolve().parent.parent)
            result = find_installations(args.folder, catalog, log) if args.folder else {"games": filter_catalog(catalog, args.query, args.references)}
        elif args.command == "extract":
            result = extract_disc_input(args.image, args.output, log)
        elif args.command == "tu-info":
            result = inspect_title_update(args.tu)
        elif args.command == "tu-extract":
            result = extract_title_update(args.tu, args.output)
        elif args.command == "prepare":
            result = prepare_game(args.game, args.tu, args.tool, args.output, log)
        else:
            _, data = load_project(args.project)
            recipe = read_recipe(args.recipe)
            if recipe.get("kind") == "unavailable":
                raise WorkbenchError(recipe.get("reason", "Recipe unavailable"))
            if recipe.get("kind") == "signature_patch":
                recipe = bind_signatures(data, recipe)
            if args.command == "scan":
                result = recipe_preview(data, recipe)
                if args.save_recipe:
                    write_json(args.save_recipe, recipe)
            else:
                result = export_candidate(args.project, recipe, args.output, log)
        print(json.dumps(result, indent=2))
    except (WorkbenchError, OSError, KeyError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
