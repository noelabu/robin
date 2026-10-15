from robin.supervisor.spool import Spool


def rec(i):
    return {"gen": "g", "session": "s", "at": 0.0, "event": {"n": i}}


def test_append_assigns_global_seq_and_replays_after(tmp_path):
    sp = Spool(tmp_path)
    for i in range(5):
        sp.append(rec(i))
    assert [r["seq"] for r in sp.after(2)] == [3, 4, 5]


def test_seq_continues_across_instances_and_unacked_survive(tmp_path):
    sp = Spool(tmp_path)
    for i in range(3):
        sp.append(rec(i))
    sp.close()
    sp2 = Spool(tmp_path)
    assert [r["seq"] for r in sp2.after(0)] == [1, 2, 3]
    assert sp2.append(rec(9))["seq"] == 4


def test_ack_deletes_fully_acknowledged_segments(tmp_path):
    sp = Spool(tmp_path, segment_bytes=200)
    for i in range(20):
        sp.append(rec(i))
    before = len(list(tmp_path.glob("segment-*")))
    sp.ack(15)
    assert len(list(tmp_path.glob("segment-*"))) < before
    assert [r["seq"] for r in sp.after(15)] == [16, 17, 18, 19, 20]
    sp.close()
    assert Spool(tmp_path).acked == 15


def test_torn_final_line_ignored_on_load(tmp_path):
    sp = Spool(tmp_path)
    sp.append(rec(1))
    sp.close()
    seg = next(tmp_path.glob("segment-*"))
    with open(seg, "ab") as f:
        f.write(b'{"seq": 2, "gen"')
    assert [r["seq"] for r in Spool(tmp_path).after(0)] == [1]


def test_size_bound_drops_oldest_and_records_gap(tmp_path):
    sp = Spool(tmp_path, segment_bytes=150, max_bytes=600)
    for i in range(40):
        sp.append(rec(i))
    assert sp.total_bytes() <= 600 + 150
    assert sp.dropped and sp.dropped[0][0] == 1
    assert sp.after(0)[0]["seq"] > 1


def test_seq_stays_monotonic_after_everything_was_acked_and_trimmed(tmp_path):
    sp = Spool(tmp_path, segment_bytes=150)
    for i in range(10):
        sp.append(rec(i))
    sp.ack(10)
    sp.close()
    Spool(tmp_path).close()  # a quiet restart: loads, trims the last acknowledged segment
    sp3 = Spool(tmp_path)
    assert not sp3.after(0)
    assert sp3.append(rec(99))["seq"] == 11
