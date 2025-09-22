import services.media_tools as media_tools


def test_enforce_ar_no_bars_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd

    monkeypatch.setattr(media_tools, "_run_sync", fake_run)
    monkeypatch.setattr(media_tools, "_has_audio", lambda _: True)
    monkeypatch.setattr(media_tools, "_ffmpeg_path", lambda: "ffmpeg")

    media_tools.enforce_ar_no_bars(tmp_path / "src.mp4", tmp_path / "dst.mp4", "16:9")

    cmd = captured["cmd"]
    vf_arg = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf_arg
    assert "crop=1920:1080" in vf_arg
    assert cmd[-1] == str(tmp_path / "dst.mp4")


def test_build_vertical_blurpad_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd

    monkeypatch.setattr(media_tools, "_run_sync", fake_run)
    monkeypatch.setattr(media_tools, "_has_audio", lambda _: False)
    monkeypatch.setattr(media_tools, "_ffmpeg_path", lambda: "ffmpeg")

    media_tools.build_vertical_blurpad(tmp_path / "src.mp4", tmp_path / "dst.mp4")

    cmd = captured["cmd"]
    fc_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "boxblur=20:1" in fc_arg
    assert "overlay=(W-w)/2:(H-h)/2" in fc_arg
    map_index = cmd.index("-map")
    assert cmd[map_index + 1] == "[vout]"
    assert cmd[-1] == str(tmp_path / "dst.mp4")
