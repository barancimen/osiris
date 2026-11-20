import subprocess
import sys
import time

from osiris.store import archiving, is_archiving


def test_the_archive_is_marked_busy_while_a_command_writes(tmp_path):
    assert not is_archiving(tmp_path)  # no mark yet
    with archiving(tmp_path):
        assert is_archiving(tmp_path)
        with archiving(tmp_path):  # several commands at once
            assert is_archiving(tmp_path)
    assert not is_archiving(tmp_path)


def test_the_mark_goes_with_the_process_however_it_ends(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", f"import time\nfrom osiris.store import archiving\n"
                               f"with archiving({str(tmp_path)!r}):\n    print('held', flush=True)\n    time.sleep(60)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert is_archiving(tmp_path)
    finally:
        holder.kill()
        holder.wait()
    deadline = time.monotonic() + 5
    while is_archiving(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not is_archiving(tmp_path)
