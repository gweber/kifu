"""Entry point. `kifu hook …` is dispatched before anything heavy is imported: hooks run at every session start."""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        from .hooks import main as hook_main
        sys.exit(hook_main(argv[1:]))
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    main()
