# -*- coding: utf-8 -*-
"""wxbot_tts - StepFun 语音合成（step_plan 套餐内，与 LLM/生图同 key 同域名）

实测（2026-08-18）：
  POST https://api.stepfun.com/step_plan/v1/audio/speech
  model=stepaudio-2.5-tts  voice=tianmeinvsheng  instruction=风格控制
  response_format=mp3 → 2.4s 出 78KB mp3（二进制流）

可用音色（实测）：cixingnansheng(磁性男声) / tianmeinvsheng(甜美女声) /
wenrounvsheng(温柔女声) / qingchunshaonv(青春少女)；instruction 支持情绪风格
（如"扮演傲娇的猫娘，语气俏皮"）。

发送形态：微信 PC 端没有原生语音气泡，走 mp3 文件卡片（wxmini2.send_file，
CF_HDROP 剪贴板粘贴），群内可内联播放。
"""
import hashlib
import os
import re
import time

DEFAULTS = {
    "enabled": True,
    # 主通道：MiniMax t2a_v2 + chunzhen_xuedi（纯真学弟，站主试听钦定 2026-08-18）
    # 备通道：StepFun stepaudio-2.5-tts（同 LLM key）
    # pitch_target_hz=0：不做升调后处理（站主要求原厂直出，处理会失真；
    #   若将来要锁音高改为 218 即启用重采样升调）
    "pitch_target_hz": 0.0,
    "keep_files": 40,
    "max_chars": 200,          # 单条语音文本上限（太长发不出去也没人听）
    "primary": {
        "provider": "minimax",
        "model": "speech-2.8-hd",
        "voice": "chunzhen_xuedi",
        "pitch": 0,                     # 原厂参数，不人为修改
        "speed": 1.0,
        "api_key_env": "WXBOT_LLM_KEY",
        "base_url": "https://api.minimaxi.com",
        "timeout_s": 90,
    },
    "fallback": {
        "provider": "stepfun",
        "model": "stepaudio-2.5-tts",
        "voice": "vibrant-youth",
        "instruction": "正太童声，10岁小男孩，声音清亮细",
        "timeout_s": 120,
    },
}


def _tcfg(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("tts") or {})
    return out


def audio_dir(cfg) -> str:
    d = (cfg.get("images") or {}).get("dir") or "wxbot_images"
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), d)
    return os.path.join(d, "audio")


def _median_f0(x, sr: int) -> float:
    """浊音帧自相关基频中位数（Hz）。x: float 单声道 [-1,1]。"""
    import numpy as np
    frame = int(sr * 0.04)
    f0s = []
    for i in range(0, len(x) - frame, frame // 2):
        seg = x[i:i + frame]
        if float(np.sqrt((seg ** 2).mean())) < 0.02:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[frame - 1:]
        ac = ac / (ac[0] + 1e-9)
        lo, hi = int(sr / 400), int(sr / 70)   # 70~400Hz 搜索窗
        if hi >= len(ac):
            continue
        peak = lo + int(np.argmax(ac[lo:hi]))
        if ac[peak] > 0.35:
            f0s.append(sr / peak)
    return float(np.median(f0s)) if f0s else 0.0


def _ensure_pitch(pcm: bytes, target_hz: float):
    """wav 字节 → (处理后的 wav 字节, 实测F0, 是否升调)。
    TTS 同配置音高波动大（实测 140~212Hz），instruction 压不住——低于目标就
    线性插值重采样升调（音频等比变短=加速=升调；instruction 已带'语速稍慢'预补偿）。"""
    import io
    import numpy as np
    import wave
    with wave.open(io.BytesIO(pcm), "rb") as w:
        sr, nch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        return pcm, 0.0, 1.0
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    f0 = _median_f0(x, sr)
    if target_hz <= 0 or f0 <= 0 or f0 >= target_hz * 0.97:
        return pcm, f0, 1.0
    ratio = min(target_hz / f0, 1.6)
    n_out = max(1, int(len(x) / ratio))
    y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    y = np.clip(y, -1.0, 1.0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())
    return buf.getvalue(), f0, ratio


def _synthesize_minimax(cfg, t, text):
    """主通道：MiniMax t2a_v2，原厂参数直出（chunzhen_xuedi，pitch=0），
    不做任何后处理（站主要求：处理会失真）。pitch_target_hz>0 时才启用升调。"""
    p = t.get("primary") or {}
    from curl_cffi import requests as creq
    key = os.environ.get(p.get("api_key_env", "WXBOT_LLM_KEY"), "")
    if not key:
        raise RuntimeError("no MiniMax api key")
    r = creq.post(
        f"{p.get('base_url', 'https://api.minimaxi.com').rstrip('/')}/v1/t2a_v2",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": p.get("model", "speech-2.8-hd"), "text": text, "stream": False,
              "voice_setting": {"voice_id": p.get("voice", "chunzhen_xuedi"),
                                "speed": float(p.get("speed", 1.0)), "vol": 1.0,
                                "pitch": int(p.get("pitch", 0))},
              "audio_setting": {"sample_rate": 24000, "bitrate": 128000,
                                "format": "wav", "channel": 1}},
        impersonate="chrome", timeout=int(p.get("timeout_s", 90)))
    d = r.json()
    audio_hex = (d.get("data") or {}).get("audio", "")
    status = ((d.get("base_resp") or {}).get("status_code"))
    if r.status_code != 200 or status not in (0, None) or not audio_hex:
        raise RuntimeError(f"minimax tts failed: {str(d.get('base_resp', {}).get('status_msg', ''))[:100]}")
    content = bytes.fromhex(audio_hex)
    if len(content) < 1000:
        raise RuntimeError("minimax tts 音频过短")
    target = float(t.get("pitch_target_hz", 0) or 0)
    if target > 0:
        content, f0, ratio = _ensure_pitch(content, target)
        print(f"[tts:minimax] F0={f0:.0f}Hz" + (f" -> x{ratio:.2f}" if ratio > 1.0 else ""))
    else:
        print(f"[tts:minimax] 原厂直出（{p.get('voice', 'chunzhen_xuedi')}）")
    return content


def _synthesize_stepfun(cfg, t, text):
    """备通道：StepFun stepaudio-2.5-tts。MiniMax 挂了才走。"""
    fb = t.get("fallback") or {}
    from curl_cffi import requests as creq
    key = cfg["llm"].get("api_key") or os.environ.get(cfg["llm"].get("api_key_env", ""))
    if not key:
        raise RuntimeError("no StepFun api key")
    r = creq.post(
        "https://api.stepfun.com/step_plan/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": fb.get("model", "stepaudio-2.5-tts"), "input": text,
              "voice": fb.get("voice", "vibrant-youth"),
              "instruction": fb.get("instruction", "正太童声，10岁小男孩，声音清亮细"),
              "response_format": "wav"},
        impersonate="chrome", timeout=int(fb.get("timeout_s", 120)))
    if r.status_code != 200:
        raise RuntimeError(f"tts HTTP {r.status_code}: {r.text[:120]}")
    if len(r.content) < 1000:
        raise RuntimeError(f"tts 音频过短({len(r.content)}B)，可能合成失败")
    target = float(t.get("pitch_target_hz", 0) or 0)
    if target > 0:
        content, f0, ratio = _ensure_pitch(r.content, target)
        print(f"[tts:stepfun] F0={f0:.0f}Hz" + (f" -> x{ratio:.2f}" if ratio > 1.0 else ""))
        return content
    print(f"[tts:stepfun] 原厂直出（{fb.get('voice', 'vibrant-youth')}）")
    return r.content


def _strip_meow(text: str) -> str:
    """语音朗读文本去猫化（站主约束：语音里不说"喵"语气词，文字人设不受影响）。
    顺序：先把"本喵/喵爷/喵呜"等自称换成"我"（否则剥语气词会念出"本"字），
    再剥所有"喵"语气词（句中/句尾/句首都可能），最后清理孤标点与重复。"""
    text = re.sub(r"本喵|喵爷|喵呜", "我", text)
    text = re.sub(r"喵+", "", text)
    text = re.sub(r"\s+", " ", text)
    # 清理孤悬标点与重复标点
    text = re.sub(r"我(我+)", "我", text)
    text = re.sub(r"[，,、]\s*[。！？!?]", "。", text)
    text = re.sub(r"^[，,、。；;~…]+", "", text.strip())
    text = re.sub(r"[，,、]\s*$", "", text.strip())
    text = re.sub(r"我[，,]\s*我", "我", text)  # "喵呜，大家好，我是" → "我，我是"类
    return text.strip()


def synthesize(cfg, text: str, voice: str = None, instruction: str = None):
    """合成一条语音并落盘 wxbot_images/audio/gen_a_<hash>.wav。
    主备双通道：MiniMax（主，原厂参数直出）→ StepFun（备）。
    pitch_target_hz=0 时全程不做升调处理。返回 (绝对路径, stem)；失败抛异常。"""
    t = _tcfg(cfg)
    if not t["enabled"]:
        raise RuntimeError("tts disabled in config")
    text = re.sub(r"\s+", " ", (text or "")).strip()[:t["max_chars"]]
    if not text:
        raise RuntimeError("text is empty")
    # 括号动作描写（猫设回复常有）念出来很怪，剥掉
    text = re.sub(r"[（(][^）)]{1,20}[）)]", "", text).strip()
    # 语音不说喵语气词（自称同步换"我"）
    text = _strip_meow(text)
    if not text:
        raise RuntimeError("剥掉动作描写和语气词后没有可念的文本")

    try:
        content = _synthesize_minimax(cfg, t, text)
    except Exception as e:
        fb = t.get("fallback") or {}
        if not fb.get("enabled", True):
            raise
        print(f"[tts] minimax failed ({str(e)[:100]}), falling back to stepfun")
        content = _synthesize_stepfun(cfg, t, text)

    d = audio_dir(cfg)
    os.makedirs(d, exist_ok=True)
    stem = "gen_a_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(d, f"{stem}.wav")
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"{stem}_{n}.wav")
        n += 1
    with open(path, "wb") as f:
        f.write(content)
    _cleanup(d, int(t["keep_files"]))
    return path, os.path.splitext(os.path.basename(path))[0]


def _cleanup(d: str, keep: int):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith((".mp3", ".wav"))),
                       key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wxbot
    t0 = time.time()
    p, stem = synthesize(wxbot.load_config(),
                         sys.argv[1] if len(sys.argv) > 1 else "（飞机耳）哼唧，本喵会说话了喵，喵喵喵")
    print(f"OK {time.time() - t0:.1f}s -> {p} (stem={stem})")
