"""Print2PACS entry point."""

import sys


def main():
    # Self-update helper verb: the downloaded exe is relaunched as
    #   Print2PACS.exe apply-update <target_exe> <old_pid>
    # to copy itself over the running exe once it exits, then relaunch.
    if len(sys.argv) >= 4 and sys.argv[1] == "apply-update":
        from print2pacs import updater
        raise SystemExit(updater.run_apply_update(sys.argv[2], int(sys.argv[3])))

    from print2pacs.gui.app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
