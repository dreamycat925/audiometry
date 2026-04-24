"""
簡易聴力スクリーニング Streamlit アプリ

目的:
- 神経心理検査の前に、聴覚入力の問題がありそうかを短時間で確認する。
- 非校正ヘッドホンでは dB HL ではなく app-dB として扱う。
- 検査者が画面を見ながら、被験者の口頭・挙手反応を入力することを前提とする。

注意:
- 診断用の純音聴力検査、障害認定、補聴器適合には使用しない。
- dB HL として出す場合は、同一PC・同一ヘッドホン・同一音量で院内校正プロファイルを作成してから使う。
"""

from __future__ import annotations

import copy
import html
import io
import json
import math
import wave
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# -----------------------------
# 基本設定
# -----------------------------
APP_TITLE = "簡易聴力スクリーニング"
FS = 44_100
DURATION_SEC = 0.75
RAMP_SEC = 0.05
DEFAULT_START_LEVEL = 40
DEFAULT_MIN_LEVEL = 0
DEFAULT_MAX_LEVEL = 80
DEFAULT_MAX_DBFS = -8.0  # MAX_LEVEL app-dB のときのデジタルピークレベル
APP_DB_REFERENCE_MAX_LEVEL = DEFAULT_MAX_LEVEL  # app-dB の音量基準は常に固定する
MAX_SAFE_APP_DB = int(math.floor(APP_DB_REFERENCE_MAX_LEVEL - DEFAULT_MAX_DBFS))

FREQ_SEQUENCE = [1000, 2000, 4000, 500, 1000]
FREQ_LABELS = ["1000Hz", "2000Hz", "4000Hz", "500Hz", "1000Hz再測定"]
CORE_FREQS = [500, 1000, 2000, 4000]
EAR_OPTIONS = {
    "右→左": ["右", "左"],
    "左→右": ["左", "右"],
}

RAW_COLUMNS = [
    "run_no",
    "test_id",
    "item_no",
    "ear",
    "freq_hz",
    "measure_type",
    "threshold_app_db",
    "censored",
    "trials",
    "completed_at",
]

LOG_COLUMNS = [
    "run_no",
    "test_id",
    "event",
    "item_no",
    "ear",
    "freq_hz",
    "measure_type",
    "level_app_db",
    "heard",
    "phase",
    "timestamp",
]

UNDO_STATE_KEYS = [
    "mode",
    "last_feedback",
    "ui_error",
    "undo_stack",
    "current_run_no",
    "active_settings",
    "active_calibration",
    "order",
    "idx",
    "results",
    "trial",
    "latest_run",
    "run_history",
    "logs",
]


# -----------------------------
# 音生成
# -----------------------------
def level_to_dbfs(level_app_db: float, max_level: float, max_dbfs: float = DEFAULT_MAX_DBFS) -> float:
    """app-dB をデジタル音量 dBFS に変換する。これは dB HL ではない。"""
    return max_dbfs - (max_level - level_app_db)


def make_tone_wav(
    freq_hz: int,
    level_app_db: float,
    ear: str,
    max_level: float,
    duration_sec: float = DURATION_SEC,
    sample_rate: int = FS,
    max_dbfs: float = DEFAULT_MAX_DBFS,
) -> bytes:
    """片耳提示用のステレオ WAV バイト列を作る。"""
    if freq_hz <= 0:
        raise ValueError("freq_hz must be positive")
    if ear not in ("右", "左", "両耳"):
        raise ValueError("ear must be '右', '左', or '両耳'")
    if level_app_db > MAX_SAFE_APP_DB:
        raise ValueError(f"level_app_db must be <= {MAX_SAFE_APP_DB} to avoid digital clipping")

    n_samples = int(sample_rate * duration_sec)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate

    dbfs = level_to_dbfs(level_app_db, max_level=max_level, max_dbfs=max_dbfs)
    amp = 10 ** (dbfs / 20.0)
    tone = amp * np.sin(2.0 * np.pi * freq_hz * t)

    # クリック音を避けるためのフェードイン・フェードアウト
    ramp_n = max(1, int(sample_rate * RAMP_SEC))
    env = np.ones(n_samples, dtype=np.float64)
    env[:ramp_n] = np.linspace(0.0, 1.0, ramp_n)
    env[-ramp_n:] = np.linspace(1.0, 0.0, ramp_n)
    tone *= env

    stereo = np.zeros((n_samples, 2), dtype=np.float64)
    if ear == "左":
        stereo[:, 0] = tone
    elif ear == "右":
        stereo[:, 1] = tone
    else:
        stereo[:, 0] = tone
        stereo[:, 1] = tone

    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# -----------------------------
# UI補助
# -----------------------------
def inject_css() -> None:
    st.markdown(
        """
<style>
.big-display-card {
    border-radius: 22px;
    padding: 1.15rem 1.3rem 1.1rem 1.3rem;
    background: linear-gradient(135deg, #f9fafb 0%, #eff6ff 100%);
    border: 1px solid #bfdbfe;
    min-height: 220px;
}
.big-display-card.level {
    background: linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%);
    border-color: #fdba74;
}
.big-display-label {
    font-size: 1.05rem;
    font-weight: 700;
    color: #374151;
    margin-bottom: 0.85rem;
}
.big-display-value-primary {
    font-size: clamp(3.4rem, 8vw, 5.2rem);
    line-height: 0.95;
    font-weight: 800;
    color: #1d4ed8;
    letter-spacing: -0.04em;
}
.big-display-value-level {
    font-size: clamp(3.6rem, 8vw, 5.4rem);
    line-height: 0.95;
    font-weight: 800;
    color: #c2410c;
    letter-spacing: -0.04em;
}
.big-display-sub {
    margin-top: 0.8rem;
    color: #4b5563;
    font-size: 0.98rem;
    line-height: 1.4;
}
.summary-card {
    border-radius: 16px;
    padding: 1rem 1.05rem;
    border: 1px solid #e5e7eb;
    background: #fafafa;
    min-height: 150px;
}
.summary-title {
    font-size: 1rem;
    font-weight: 700;
    color: #374151;
    margin-bottom: 0.25rem;
}
.summary-result {
    font-size: 1.75rem;
    font-weight: 800;
    margin: 0 0 0.5rem 0;
}
.summary-meta {
    font-size: 0.95rem;
    line-height: 1.45;
    color: #4b5563;
}
.summary-neutral .summary-result {
    color: #6b7280;
}
.summary-good .summary-result {
    color: #15803d;
}
.summary-caution .summary-result {
    color: #b45309;
}
.summary-alert .summary-result {
    color: #b91c1c;
}
</style>
""",
        unsafe_allow_html=True,
    )


def render_big_display(title: str, value: str, subtitle: str = "", kind: str = "primary") -> None:
    value_class = "big-display-value-level" if kind == "level" else "big-display-value-primary"
    extra_class = " level" if kind == "level" else ""
    subtitle_html = f'<div class="big-display-sub">{html.escape(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f"""
<div class="big-display-card{extra_class}">
    <div class="big-display-label">{html.escape(title)}</div>
    <div class="{value_class}">{html.escape(value)}</div>
    {subtitle_html}
</div>
""",
        unsafe_allow_html=True,
    )


def render_summary_card(title: str, result: str, lines: List[str], tone: str = "neutral") -> None:
    st.markdown(
        f"""
<div class="summary-card summary-{html.escape(tone)}">
    <div class="summary-title">{html.escape(title)}</div>
    <div class="summary-result">{html.escape(result)}</div>
    <div class="summary-meta">{'<br>'.join(html.escape(line) for line in lines if line)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


# -----------------------------
# 状態管理
# -----------------------------
def init_state() -> None:
    defaults: Dict[str, Any] = {
        "mode": "idle",
        "last_feedback": None,
        "ui_error": None,
        "undo_stack": [],
        "current_run_no": 0,
        "active_settings": None,
        "active_calibration": None,
        "order": [],
        "idx": 0,
        "results": [],
        "trial": None,
        "latest_run": None,
        "run_history": [],
        "logs": [],
        "test_id_input": "",
        "headphone_input": "",
        "device_note_input": "OS音量100%、ブラウザ音量100%",
        "environment_note_input": "静かな個室。ヘッドホン左右確認済み。",
        "ear_order_input": "右→左",
        "start_level_input": DEFAULT_START_LEVEL,
        "min_level_input": DEFAULT_MIN_LEVEL,
        "max_level_input": DEFAULT_MAX_LEVEL,
        "autoplay_input": True,
        "use_calibration_input": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_active_test_state() -> None:
    st.session_state["mode"] = "idle"
    st.session_state["active_settings"] = None
    st.session_state["active_calibration"] = None
    st.session_state["order"] = []
    st.session_state["idx"] = 0
    st.session_state["results"] = []
    st.session_state["trial"] = None
    st.session_state["last_feedback"] = None
    st.session_state["ui_error"] = None


def reset_all() -> None:
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)
    init_state()


def settings_snapshot() -> Dict[str, Any]:
    return {
        "test_id": str(st.session_state.get("test_id_input", "")),
        "headphone": str(st.session_state.get("headphone_input", "")),
        "device_note": str(st.session_state.get("device_note_input", "")),
        "environment_note": str(st.session_state.get("environment_note_input", "")),
        "ear_order": str(st.session_state.get("ear_order_input", "右→左")),
        "start_level": int(st.session_state.get("start_level_input", DEFAULT_START_LEVEL)),
        "min_level": int(st.session_state.get("min_level_input", DEFAULT_MIN_LEVEL)),
        "max_level": int(st.session_state.get("max_level_input", DEFAULT_MAX_LEVEL)),
        "autoplay": bool(st.session_state.get("autoplay_input", True)),
        "use_calibration": bool(st.session_state.get("use_calibration_input", False)),
    }


def validate_settings(settings: Dict[str, Any], calibration: Optional[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    if settings["max_level"] <= settings["min_level"]:
        errors.append("最大レベルは最小レベルより大きくしてください。")
    if not (settings["min_level"] <= settings["start_level"] <= settings["max_level"]):
        errors.append("開始レベルは最小〜最大レベルの範囲内にしてください。")
    if settings["max_level"] > MAX_SAFE_APP_DB:
        errors.append(
            f"最大レベルは {MAX_SAFE_APP_DB} app-dB 以下にしてください。"
            " 現在の内部スケールでは、それを超えるとデジタルクリップの恐れがあります。"
        )
    if settings.get("use_calibration") and calibration is None:
        errors.append("校正プロファイルを使う場合は、有効なJSONをアップロードしてください。")
    return errors


def build_order(ear_order_label: str) -> List[Dict[str, Any]]:
    """検査項目リストを作る。"""
    ears = EAR_OPTIONS[ear_order_label]
    order: List[Dict[str, Any]] = []
    item_no = 1
    for ear in ears:
        for freq, label in zip(FREQ_SEQUENCE, FREQ_LABELS):
            measure_type = "retest" if label.endswith("再測定") else "main"
            order.append(
                {
                    "item_no": item_no,
                    "ear": ear,
                    "freq_hz": int(freq),
                    "label": label,
                    "measure_type": measure_type,
                }
            )
            item_no += 1
    return order


def new_trial(start_level: int, min_level: int, max_level: int) -> Dict[str, Any]:
    return {
        "level": int(start_level),
        "phase": "init",
        "last_no": None,
        "trials": [],
        "min_level": int(min_level),
        "max_level": int(max_level),
    }


def start_test(settings: Dict[str, Any], calibration: Optional[Dict[str, Any]]) -> None:
    st.session_state["current_run_no"] = int(st.session_state.get("current_run_no", 0)) + 1
    st.session_state["mode"] = "running"
    st.session_state["last_feedback"] = None
    st.session_state["ui_error"] = None
    st.session_state["undo_stack"] = []
    st.session_state["active_settings"] = settings
    st.session_state["active_calibration"] = calibration
    st.session_state["order"] = build_order(settings["ear_order"])
    st.session_state["idx"] = 0
    st.session_state["results"] = []
    st.session_state["trial"] = new_trial(
        settings["start_level"], settings["min_level"], settings["max_level"]
    )


def push_undo_snapshot() -> None:
    snapshot = {key: copy.deepcopy(st.session_state.get(key)) for key in UNDO_STATE_KEYS if key != "undo_stack"}
    undo_stack = list(st.session_state.get("undo_stack") or [])
    undo_stack.append(snapshot)
    st.session_state["undo_stack"] = undo_stack


def can_undo_last_answer() -> bool:
    return bool(st.session_state.get("undo_stack"))


def undo_last_answer() -> None:
    undo_stack = list(st.session_state.get("undo_stack") or [])
    if not undo_stack:
        return
    snapshot = undo_stack.pop()
    for key, value in snapshot.items():
        st.session_state[key] = value
    st.session_state["undo_stack"] = undo_stack
    st.session_state["last_feedback"] = "直前の入力を取り消しました。"


def current_item() -> Dict[str, Any]:
    return st.session_state["order"][st.session_state["idx"]]


def append_log_row(row: Dict[str, Any]) -> None:
    logs = list(st.session_state.get("logs") or [])
    logs.append(row)
    st.session_state["logs"] = logs


def raw_df_from_results(
    results: List[Dict[str, Any]],
    run_no: int,
    settings: Optional[Dict[str, Any]],
) -> pd.DataFrame:
    df = pd.DataFrame(results)
    if df.empty:
        return pd.DataFrame(columns=RAW_COLUMNS)
    df["run_no"] = int(run_no)
    df["test_id"] = str((settings or {}).get("test_id", ""))
    return df[RAW_COLUMNS]


def log_df(run_no: Optional[int] = None) -> pd.DataFrame:
    logs = list(st.session_state.get("logs") or [])
    df = pd.DataFrame(logs)
    if df.empty:
        return pd.DataFrame(columns=LOG_COLUMNS)
    for col in LOG_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    if run_no is not None:
        df = df[df["run_no"] == int(run_no)]
    return df[LOG_COLUMNS]


def record_threshold(threshold: int, censored: bool = False) -> None:
    item = current_item()
    trial = st.session_state["trial"]
    settings = st.session_state.get("active_settings") or {}
    run_no = int(st.session_state.get("current_run_no", 0))
    result = {
        "item_no": item["item_no"],
        "ear": item["ear"],
        "freq_hz": item["freq_hz"],
        "measure_type": item["measure_type"],
        "threshold_app_db": int(threshold),
        "censored": bool(censored),
        "trials": json.dumps(trial["trials"], ensure_ascii=False),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    results = list(st.session_state.get("results") or [])
    results.append(result)
    st.session_state["results"] = results

    append_log_row(
        {
            "run_no": run_no,
            "test_id": str(settings.get("test_id", "")),
            "event": "threshold_recorded",
            "item_no": item["item_no"],
            "ear": item["ear"],
            "freq_hz": item["freq_hz"],
            "measure_type": item["measure_type"],
            "level_app_db": int(threshold),
            "heard": None,
            "phase": "censored" if censored else "threshold",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
    )

    st.session_state["idx"] = int(st.session_state.get("idx", 0)) + 1
    order = list(st.session_state.get("order") or [])
    if st.session_state["idx"] >= len(order):
        finalize_run()
        return

    st.session_state["trial"] = new_trial(
        settings["start_level"], settings["min_level"], settings["max_level"]
    )


def finalize_run() -> None:
    settings = copy.deepcopy(st.session_state.get("active_settings") or {})
    calibration = copy.deepcopy(st.session_state.get("active_calibration"))
    run_no = int(st.session_state.get("current_run_no", 0))
    results = copy.deepcopy(list(st.session_state.get("results") or []))
    raw_df = raw_df_from_results(results, run_no, settings)
    summary_df = summarize(raw_df, calibration)
    report_note = generate_neuropsych_note(summary_df, calibration)
    summary_text = make_summary_text(summary_df, settings, calibration, run_no=run_no)
    text_report = make_text_report(raw_df, summary_df, settings, calibration, run_no=run_no)
    completed_at = datetime.now().isoformat(timespec="seconds")

    run_record = {
        "run_no": run_no,
        "completed_at": completed_at,
        "settings": settings,
        "calibration": calibration,
        "raw_rows": results,
        "summary_rows": summary_df.to_dict(orient="records"),
        "report_note": report_note,
        "summary_text": summary_text,
        "text_report": text_report,
    }

    history = list(st.session_state.get("run_history") or [])
    history.append(run_record)
    st.session_state["run_history"] = history
    st.session_state["latest_run"] = run_record
    st.session_state["mode"] = "idle"
    st.session_state["active_settings"] = None
    st.session_state["active_calibration"] = None
    st.session_state["order"] = []
    st.session_state["idx"] = 0
    st.session_state["results"] = []
    st.session_state["trial"] = None
    st.session_state["last_feedback"] = f"Run {run_no} を終了しました。"


def respond(heard: bool) -> None:
    """
    速い上昇法。

    1) 開始レベルで聞こえれば10dBずつ下げ、聞こえなくなったら5dBずつ上げる。
    2) 開始レベルで聞こえなければ10dBずつ上げ、初回反応後に直前の不反応+5dBから確認する。
    3) 上昇系列で最初に聞こえたレベルを閾値とする。
    """
    if st.session_state.get("mode") != "running":
        return

    push_undo_snapshot()

    trial = st.session_state["trial"]
    item = current_item()
    settings = st.session_state.get("active_settings") or {}
    run_no = int(st.session_state.get("current_run_no", 0))
    level = int(trial["level"])
    phase = trial["phase"]
    min_level = int(trial["min_level"])
    max_level = int(trial["max_level"])

    append_log_row(
        {
            "run_no": run_no,
            "test_id": str(settings.get("test_id", "")),
            "event": "response",
            "item_no": item["item_no"],
            "ear": item["ear"],
            "freq_hz": item["freq_hz"],
            "measure_type": item["measure_type"],
            "level_app_db": level,
            "heard": bool(heard),
            "phase": phase,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
    )

    trial["trials"].append({"level": level, "heard": bool(heard), "phase": phase})

    if heard and level <= min_level and phase in {"init", "down"}:
        record_threshold(min_level, censored=False)
        return

    if not heard and level >= max_level:
        record_threshold(max_level + 5, censored=True)
        return

    if phase == "init":
        if heard:
            trial["phase"] = "down"
            trial["level"] = max(min_level, level - 10)
        else:
            trial["phase"] = "coarse_up"
            trial["last_no"] = level
            trial["level"] = min(max_level, level + 10)
        return

    if phase == "down":
        if heard:
            trial["level"] = max(min_level, level - 10)
        else:
            trial["phase"] = "fine_up"
            trial["last_no"] = level
            trial["level"] = min(max_level, level + 5)
        return

    if phase == "coarse_up":
        if heard:
            last_no = trial.get("last_no")
            if last_no is None:
                record_threshold(level, censored=False)
            else:
                trial["phase"] = "fine_up"
                trial["level"] = min(max_level, int(last_no) + 5)
        else:
            trial["last_no"] = level
            trial["level"] = min(max_level, level + 10)
        return

    if phase == "fine_up":
        if heard:
            record_threshold(level, censored=False)
        else:
            trial["last_no"] = level
            trial["level"] = min(max_level, level + 5)
        return

    raise RuntimeError(f"Unknown phase: {phase}")


# -----------------------------
# 校正プロファイル
# -----------------------------
def parse_calibration(uploaded_file: Optional[Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if uploaded_file is None:
        return None, None
    try:
        data = json.load(uploaded_file)
        offsets = data.get("offsets_db", {})
        for ear in ["右", "左"]:
            if ear not in offsets:
                raise ValueError(f"offsets_db に {ear} がありません")
            for freq in CORE_FREQS:
                if str(freq) not in offsets[ear]:
                    raise ValueError(f"offsets_db['{ear}']['{freq}'] がありません")
                float(offsets[ear][str(freq)])
        return data, None
    except Exception as exc:  # noqa: BLE001
        return None, f"校正プロファイルを読み込めませんでした: {exc}"


def get_offset(calibration: Optional[Dict[str, Any]], ear: str, freq_hz: int) -> float:
    if not calibration:
        return 0.0
    return float(calibration["offsets_db"][ear][str(freq_hz)])


def apply_calibration(value_app_db: float, calibration: Optional[Dict[str, Any]], ear: str, freq_hz: int) -> float:
    return float(value_app_db) + get_offset(calibration, ear, freq_hz)


# -----------------------------
# 集計・レポート
# -----------------------------
def _main_result(df: pd.DataFrame, ear: str, freq_hz: int) -> Optional[pd.Series]:
    sub = df[
        (df["ear"] == ear)
        & (df["freq_hz"] == freq_hz)
        & (df["measure_type"] == "main")
    ]
    if sub.empty:
        return None
    return sub.iloc[0]


def _retest_result(df: pd.DataFrame, ear: str, freq_hz: int = 1000) -> Optional[pd.Series]:
    sub = df[
        (df["ear"] == ear)
        & (df["freq_hz"] == freq_hz)
        & (df["measure_type"] == "retest")
    ]
    if sub.empty:
        return None
    return sub.iloc[0]


def summarize(df: pd.DataFrame, calibration: Optional[Dict[str, Any]]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    for ear in ["右", "左"]:
        row: Dict[str, Any] = {"耳": ear}
        app_values: Dict[int, float] = {}
        est_values: Dict[int, float] = {}
        censored_freqs: List[int] = []

        for freq in CORE_FREQS:
            result = _main_result(df, ear, freq)
            if result is None:
                row[f"{freq}Hz_app_dB"] = math.nan
                row[f"{freq}Hz_表示"] = ""
                if calibration:
                    row[f"{freq}Hz_est_dBHL"] = math.nan
                continue

            value = float(result["threshold_app_db"])
            censored = bool(result["censored"])
            if censored:
                censored_freqs.append(freq)
                row[f"{freq}Hz_app_dB"] = math.nan
                row[f"{freq}Hz_表示"] = f">={int(value)} app-dB"
                if calibration:
                    est = apply_calibration(value, calibration, ear, freq)
                    row[f"{freq}Hz_est_dBHL"] = math.nan
                    row[f"{freq}Hz_推定表示"] = f">={est:.1f} dB HL"
                continue

            app_values[freq] = value
            row[f"{freq}Hz_app_dB"] = round(value, 1)
            row[f"{freq}Hz_表示"] = f"{value:.1f} app-dB"
            if calibration:
                est = apply_calibration(value, calibration, ear, freq)
                est_values[freq] = est
                row[f"{freq}Hz_est_dBHL"] = round(est, 1)
                row[f"{freq}Hz_推定表示"] = f"{est:.1f} dB HL"

        if all(freq in app_values for freq in [500, 1000, 2000]):
            row["旧来4分法_app_dB"] = round(
                (app_values[500] + 2 * app_values[1000] + app_values[2000]) / 4.0, 1
            )
        else:
            row["旧来4分法_app_dB"] = math.nan

        if all(freq in app_values for freq in CORE_FREQS):
            row["4周波数平均_app_dB"] = round(
                sum(app_values[freq] for freq in CORE_FREQS) / 4.0, 1
            )
        else:
            row["4周波数平均_app_dB"] = math.nan

        if calibration:
            if all(freq in est_values for freq in [500, 1000, 2000]):
                row["旧来4分法_est_dBHL"] = round(
                    (est_values[500] + 2 * est_values[1000] + est_values[2000]) / 4.0, 1
                )
            else:
                row["旧来4分法_est_dBHL"] = math.nan

            if all(freq in est_values for freq in CORE_FREQS):
                row["4周波数平均_est_dBHL"] = round(
                    sum(est_values[freq] for freq in CORE_FREQS) / 4.0, 1
                )
            else:
                row["4周波数平均_est_dBHL"] = math.nan

        main_1000_result = _main_result(df, ear, 1000)
        retest_1000_result = _retest_result(df, ear, 1000)
        if (
            main_1000_result is not None
            and retest_1000_result is not None
            and not bool(main_1000_result["censored"])
            and not bool(retest_1000_result["censored"])
        ):
            diff = abs(
                float(main_1000_result["threshold_app_db"])
                - float(retest_1000_result["threshold_app_db"])
            )
            row["1000Hz再測定差_app_dB"] = round(diff, 1)
            if diff >= 10:
                row["信頼性メモ"] = "1000Hz再測定差が10dB以上"
            elif diff > 5:
                row["信頼性メモ"] = "1000Hz再測定差が5dB超"
            else:
                row["信頼性メモ"] = ""
        else:
            row["1000Hz再測定差_app_dB"] = math.nan
            row["信頼性メモ"] = ""

        if censored_freqs:
            row["打ち切りメモ"] = "最大提示で反応なし: " + ", ".join(f"{freq}Hz" for freq in censored_freqs)
        else:
            row["打ち切りメモ"] = ""

        rows.append(row)

    return pd.DataFrame(rows)


def generate_neuropsych_note(summary_df: pd.DataFrame, calibration: Optional[Dict[str, Any]]) -> str:
    if summary_df.empty:
        return "結果がありません。"

    value_col = "旧来4分法_est_dBHL" if calibration and "旧来4分法_est_dBHL" in summary_df else "旧来4分法_app_dB"
    unit = "推定dB HL" if value_col.endswith("est_dBHL") else "app-dB（非校正）"

    notes: List[str] = ["【神経心理検査用メモ】", f"本結果は {unit} による参考値である。"]
    ear_values: Dict[str, float] = {}
    for _, row in summary_df.iterrows():
        ear = str(row["耳"])
        value = row.get(value_col, math.nan)
        if pd.notna(value):
            ear_values[ear] = float(value)
            if value >= 40:
                notes.append(f"{ear}耳の旧来4分法は {value:.1f} {unit} で、聴覚提示課題の解釈に注意を要する。")
            elif value >= 30:
                notes.append(f"{ear}耳の旧来4分法は {value:.1f} {unit} で、軽度の聴覚入力低下の可能性に注意する。")

        reliability = str(row.get("信頼性メモ", ""))
        if reliability:
            notes.append(f"{ear}耳: {reliability}")

        censored = str(row.get("打ち切りメモ", ""))
        if censored:
            notes.append(f"{ear}耳: {censored}")

    if "右" in ear_values and "左" in ear_values:
        diff = abs(ear_values["右"] - ear_values["左"])
        if diff >= 15:
            notes.append(f"左右の旧来4分法差は {diff:.1f} {unit} で、左右差の可能性がある。")

    if len(notes) == 2:
        notes.append("今回の簡易スクリーニング範囲では、明らかな聴覚入力低下フラグは目立たない。")

    notes.append(
        "異常値、左右差、再測定差大、聴覚症状、語聾・聴覚失認が疑われる場合は、標準純音聴力検査および語音聴力検査等を検討する。"
    )
    return "\n".join(notes)


def make_text_report(
    raw_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    settings: Dict[str, Any],
    calibration: Optional[Dict[str, Any]],
    run_no: Optional[int] = None,
) -> str:
    lines: List[str] = ["簡易聴力スクリーニング結果", "=" * 32]
    if run_no is not None:
        lines.append(f"Run: {run_no}")
    lines.append(f"作成日時: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"検査ID: {settings.get('test_id', '')}")
    lines.append(f"ヘッドホン: {settings.get('headphone', '')}")
    lines.append(f"PC/ブラウザ/OS音量: {settings.get('device_note', '')}")
    lines.append(f"環境メモ: {settings.get('environment_note', '')}")
    lines.append(f"提示範囲: {settings.get('min_level')}〜{settings.get('max_level')} app-dB")
    if calibration:
        lines.append(f"校正プロファイル: {calibration.get('profile_name', '名称なし')}")
        lines.append("表示に推定dB HLを含む。")
    else:
        lines.append("校正プロファイル: なし。表示値は dB HL ではなく app-dB。")

    lines.append("")
    lines.append("集計")
    lines.append("-" * 32)
    lines.append(summary_df.to_string(index=False) if not summary_df.empty else "集計結果なし")
    lines.append("")
    lines.append("周波数別の生データ")
    lines.append("-" * 32)
    lines.append(raw_df.to_string(index=False) if not raw_df.empty else "生データなし")
    lines.append("")
    lines.append(generate_neuropsych_note(summary_df, calibration))
    lines.append("")
    lines.append("注記")
    lines.append("- 本検査は非校正または院内校正のPC/ブラウザ音源およびヘッドホンによる簡易聴覚スクリーニングである。")
    lines.append("- 検査者が画面を見ながら、被験者の応答を記録する方式を想定する。")
    lines.append("- 標準純音聴力検査の代替ではなく、診断、障害認定、補聴器適合には使用しない。")
    lines.append("- 神経心理検査における聴覚入力条件の確認を目的とした参考値として扱う。")
    return "\n".join(lines)


def make_summary_text(
    summary_df: pd.DataFrame,
    settings: Dict[str, Any],
    calibration: Optional[Dict[str, Any]],
    run_no: Optional[int] = None,
) -> str:
    lines: List[str] = ["簡易聴力スクリーニング サマリー", "=" * 32]
    if run_no is not None:
        lines.append(f"Run: {run_no}")
    lines.append(f"作成日時: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"検査ID: {settings.get('test_id', '')}")
    lines.append(f"ヘッドホン: {settings.get('headphone', '')}")
    lines.append(f"提示範囲: {settings.get('min_level')}〜{settings.get('max_level')} app-dB")
    lines.append(f"校正: {calibration.get('profile_name', 'なし') if calibration else 'なし'}")
    lines.append("")
    lines.append("耳別サマリー")
    lines.append("-" * 32)

    if summary_df.empty:
        lines.append("結果なし")
    else:
        for _, row in summary_df.iterrows():
            ear = str(row.get("耳", ""))
            lines.append(f"{ear}耳")
            lines.append(f"  500Hz: {row.get('500Hz_表示', '')}")
            lines.append(f"  1000Hz: {row.get('1000Hz_表示', '')}")
            lines.append(f"  2000Hz: {row.get('2000Hz_表示', '')}")
            lines.append(f"  4000Hz: {row.get('4000Hz_表示', '')}")
            avg_old = row.get("旧来4分法_app_dB", math.nan)
            avg_4 = row.get("4周波数平均_app_dB", math.nan)
            retest = row.get("1000Hz再測定差_app_dB", math.nan)
            lines.append(f"  旧来4分法: {'—' if pd.isna(avg_old) else f'{float(avg_old):.1f} app-dB'}")
            lines.append(f"  4周波数平均: {'—' if pd.isna(avg_4) else f'{float(avg_4):.1f} app-dB'}")
            lines.append(f"  1000Hz再検差: {'—' if pd.isna(retest) else f'{float(retest):.1f} app-dB'}")
            reliability = str(row.get("信頼性メモ", ""))
            censored = str(row.get("打ち切りメモ", ""))
            if reliability:
                lines.append(f"  注意: {reliability}")
            if censored:
                lines.append(f"  注意: {censored}")
            lines.append("")

    lines.append(generate_neuropsych_note(summary_df, calibration))
    return "\n".join(lines)


def latest_run_dataframes() -> Tuple[Optional[Dict[str, Any]], pd.DataFrame, pd.DataFrame]:
    latest = st.session_state.get("latest_run")
    if latest is None:
        return None, pd.DataFrame(columns=RAW_COLUMNS), pd.DataFrame()
    settings = latest.get("settings") or {}
    raw_df = raw_df_from_results(latest.get("raw_rows") or [], int(latest["run_no"]), settings)
    summary_df = pd.DataFrame(latest.get("summary_rows") or [])
    return latest, raw_df, summary_df


def build_history_dataframe() -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for entry in st.session_state.get("run_history") or []:
        summary_df = pd.DataFrame(entry.get("summary_rows") or [])
        row: Dict[str, Any] = {
            "run_no": entry.get("run_no"),
            "completed_at": entry.get("completed_at"),
            "test_id": (entry.get("settings") or {}).get("test_id", ""),
            "headphone": (entry.get("settings") or {}).get("headphone", ""),
        }
        for ear in ["右", "左"]:
            if summary_df.empty:
                row[f"{ear}_旧来4分法_app_dB"] = math.nan
                row[f"{ear}_再検差_app_dB"] = math.nan
                row[f"{ear}_注意"] = ""
                continue
            sub = summary_df[summary_df["耳"] == ear]
            if sub.empty:
                row[f"{ear}_旧来4分法_app_dB"] = math.nan
                row[f"{ear}_再検差_app_dB"] = math.nan
                row[f"{ear}_注意"] = ""
            else:
                record = sub.iloc[0]
                row[f"{ear}_旧来4分法_app_dB"] = record.get("旧来4分法_app_dB", math.nan)
                row[f"{ear}_再検差_app_dB"] = record.get("1000Hz再測定差_app_dB", math.nan)
                caution = " / ".join(
                    text
                    for text in [str(record.get("信頼性メモ", "")), str(record.get("打ち切りメモ", ""))]
                    if text
                )
                row[f"{ear}_注意"] = caution
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def build_audiogram_chart(
    raw_df: pd.DataFrame,
    min_level: int,
    max_level: int,
) -> Optional[alt.Chart]:
    if raw_df.empty:
        return None

    plot_df = raw_df[raw_df["measure_type"] == "main"].copy()
    if plot_df.empty:
        return None

    plot_df["周波数"] = plot_df["freq_hz"].astype(str) + " Hz"
    plot_df["閾値"] = plot_df["threshold_app_db"].astype(float)
    plot_df["打ち切り"] = plot_df["censored"].map({True: "打ち切り", False: "閾値"})
    freq_order = [f"{freq} Hz" for freq in CORE_FREQS]
    y_max = max(max_level + 5, int(plot_df["閾値"].max()) + 5)
    y_min = min(min_level, int(plot_df["閾値"].min()))

    base = alt.Chart(plot_df).encode(
        x=alt.X("周波数:N", sort=freq_order, title="周波数"),
        y=alt.Y(
            "閾値:Q",
            title="app-dB",
            scale=alt.Scale(domain=[y_max, y_min]),
        ),
        color=alt.Color("ear:N", title="耳"),
        tooltip=[
            alt.Tooltip("ear:N", title="耳"),
            alt.Tooltip("freq_hz:Q", title="周波数(Hz)"),
            alt.Tooltip("threshold_app_db:Q", title="閾値(app-dB)"),
            alt.Tooltip("打ち切り:N", title="状態"),
        ],
    )
    line = base.mark_line(point=False, strokeWidth=2.6)
    point = base.mark_point(filled=True, size=120).encode(shape=alt.Shape("打ち切り:N", title="状態"))

    return (
        (line + point)
        .properties(height=320)
        .configure_axis(labelFontSize=12, titleFontSize=13)
        .configure_legend(labelFontSize=12, titleFontSize=12)
    )


def latest_run_downloads() -> None:
    session_logs_df = log_df()
    st.caption(
        "CSV には run_no, item_no, ear, freq_hz, measure_type, level_app_db, heard, phase, timestamp が入ります。"
    )
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "全sessionログCSVをダウンロード",
            data=session_logs_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="hearing_session_log.csv",
            mime="text/csv",
        )
    with c2:
        if latest_run is not None:
            st.download_button(
                "最新runサマリーTXTをダウンロード",
                data=str(latest_run.get("summary_text", "")).encode("utf-8-sig"),
                file_name=f"hearing_summary_run_{latest_run['run_no']}.txt",
                mime="text/plain",
            )


def render_latest_summary_cards(summary_df: pd.DataFrame) -> None:
    if summary_df.empty:
        return
    cols = st.columns(2)
    for idx, ear in enumerate(["右", "左"]):
        sub = summary_df[summary_df["耳"] == ear]
        if sub.empty:
            continue
        row = sub.iloc[0]
        avg_text = row.get("旧来4分法_app_dB", math.nan)
        retest_text = row.get("1000Hz再測定差_app_dB", math.nan)
        warning_text = " / ".join(
            text for text in [str(row.get("信頼性メモ", "")), str(row.get("打ち切りメモ", ""))] if text
        )
        tone = "good"
        if warning_text:
            tone = "alert" if "10dB以上" in warning_text or "最大提示" in warning_text else "caution"
        if pd.isna(avg_text):
            result = "—"
        else:
            result = f"{float(avg_text):.1f} app-dB"
        four_freq_avg = row.get("4周波数平均_app_dB", math.nan)
        lines = [
            f"4周波数平均: {'—' if pd.isna(four_freq_avg) else f'{float(four_freq_avg):.1f} app-dB'}",
            f"1000Hz再検差: {'—' if pd.isna(retest_text) else f'{float(retest_text):.1f} app-dB'}",
            warning_text or "大きな注意所見なし",
        ]
        with cols[idx]:
            render_summary_card(f"{ear}耳サマリー", result, lines, tone=tone)


# -----------------------------
# アプリ本体
# -----------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="👂", layout="centered")
inject_css()
init_state()

st.title(APP_TITLE)
st.caption("検査者が画面を見ながら音を提示し、被験者の口頭・挙手反応を入力する簡易スクリーニングです。校正なしでは dB HL ではなく app-dB として扱います。")

mode = st.session_state.get("mode", "idle")
ui_locked = mode == "running"
latest_run, latest_raw_df, latest_summary_df = latest_run_dataframes()

with st.sidebar:
    st.header("⚙️ 検査設定")
    st.text_input("検査ID / 患者IDなど", key="test_id_input", disabled=ui_locked)
    st.text_input("ヘッドホン名", key="headphone_input", disabled=ui_locked)
    st.text_input("PC / ブラウザ / OS音量", key="device_note_input", disabled=ui_locked)
    st.text_area("環境メモ", key="environment_note_input", height=80, disabled=ui_locked)
    st.radio("検査順", list(EAR_OPTIONS.keys()), key="ear_order_input", horizontal=True, disabled=ui_locked)
    st.number_input("開始レベル app-dB", min_value=0, max_value=90, step=5, key="start_level_input", disabled=ui_locked)
    st.number_input("最小レベル app-dB", min_value=0, max_value=40, step=5, key="min_level_input", disabled=ui_locked)
    st.number_input(
        "最大レベル app-dB",
        min_value=40,
        max_value=MAX_SAFE_APP_DB,
        step=5,
        key="max_level_input",
        disabled=ui_locked,
        help=f"現在の内部音量スケールでは {MAX_SAFE_APP_DB} app-dB が安全上限です。",
    )
    st.checkbox("音を自動再生する", key="autoplay_input", disabled=ui_locked)

    st.divider()
    st.checkbox("院内校正プロファイルを使う", key="use_calibration_input", disabled=ui_locked)
    calibration_data = None
    calibration_error = None
    if st.session_state.get("use_calibration_input"):
        calibration_upload = st.file_uploader("校正JSONをアップロード", type=["json"], disabled=ui_locked)
        calibration_data, calibration_error = parse_calibration(calibration_upload)
        if calibration_error:
            st.error(calibration_error)
        elif calibration_data:
            st.success(f"校正プロファイル: {calibration_data.get('profile_name', '名称なし')}")

    st.divider()
    if st.button("現在の検査をリセット", disabled=mode != "running", use_container_width=True):
        reset_active_test_state()
        st.rerun()
    if st.button("全履歴を全消去", use_container_width=True):
        reset_all()
        st.rerun()

settings = settings_snapshot()
settings_errors = validate_settings(settings, calibration_data)

if mode == "idle":
    st.info("被験者には画面を見せず、検査者が反応を入力してください。")
else:
    st.success(f"Run {st.session_state.get('current_run_no', 0)} 実施中")

if st.session_state.get("ui_error"):
    st.error(str(st.session_state["ui_error"]))

if st.session_state.get("last_feedback"):
    st.write(st.session_state["last_feedback"])

action_cols = st.columns(3)
with action_cols[0]:
    start_disabled = bool(settings_errors) or mode == "running"
    if st.button("検査開始", type="primary", use_container_width=True, disabled=start_disabled):
        start_test(settings, calibration_data)
        st.rerun()
with action_cols[1]:
    if st.button("ひとつ前に戻る", use_container_width=True, disabled=not can_undo_last_answer()):
        undo_last_answer()
        st.rerun()
with action_cols[2]:
    if latest_run is not None:
        st.caption(f"最新 run: {latest_run['run_no']}")

for message in settings_errors:
    st.error(message)

if mode == "idle":
    st.subheader("実施前チェック")
    st.markdown(
        """
1. できるだけ静かな部屋で実施する。  
2. 有線ヘッドホンを推奨し、PC・OS・ブラウザの音量を固定する。  
3. 被験者には「音が聞こえたら合図してください。迷った場合は聞こえない扱いにします」と説明する。  
4. 左右確認音で、ヘッドホンの左右が合っているか確認する。  
"""
    )

    st.subheader("左右確認音")
    check_level = 60
    col_l, col_r, col_b = st.columns(3)
    with col_l:
        st.write("左")
        st.audio(make_tone_wav(1000, check_level, "左", DEFAULT_MAX_LEVEL), format="audio/wav")
    with col_r:
        st.write("右")
        st.audio(make_tone_wav(1000, check_level, "右", DEFAULT_MAX_LEVEL), format="audio/wav")
    with col_b:
        st.write("両耳")
        st.audio(make_tone_wav(1000, check_level, "両耳", DEFAULT_MAX_LEVEL), format="audio/wav")

if mode == "running":
    item = current_item()
    trial = st.session_state["trial"]
    level = int(trial["level"])
    progress = st.session_state["idx"] / len(st.session_state["order"])
    st.progress(progress)
    st.write(f"進行状況: {st.session_state['idx'] + 1} / {len(st.session_state['order'])}")

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("耳", str(item["ear"]))
    with m2:
        st.metric("周波数", f"{item['freq_hz']} Hz")
    with m3:
        st.metric("項目", str(item["label"]))
    with m4:
        st.metric("探索段階", str(trial["phase"]))

    d1, d2 = st.columns([1.0, 1.15])
    with d1:
        render_big_display(
            "次に提示する音",
            f"{item['ear']} {item['freq_hz']}Hz",
            subtitle="被験者には画面を見せず、検査者が提示してください。",
        )
    with d2:
        subtitle = "app-dB はこのアプリ内の相対レベルです。"
        if st.session_state.get("active_calibration"):
            est_level = apply_calibration(level, st.session_state["active_calibration"], item["ear"], int(item["freq_hz"]))
            subtitle = f"この条件での推定表示: {est_level:.1f} dB HL"
        render_big_display("提示レベル", f"{level} app-dB", subtitle=subtitle, kind="level")

    wav_bytes = make_tone_wav(
        freq_hz=int(item["freq_hz"]),
        level_app_db=level,
        ear=str(item["ear"]),
        max_level=APP_DB_REFERENCE_MAX_LEVEL,
    )
    st.audio(wav_bytes, format="audio/wav", autoplay=bool(settings.get("autoplay", True)))
    st.caption("自動再生されない場合は、プレイヤーの再生ボタンを押してください。")

    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("聞こえた", type="primary", use_container_width=True):
            respond(True)
            st.rerun()
    with a2:
        if st.button("聞こえない", use_container_width=True):
            respond(False)
            st.rerun()
    with a3:
        if st.button("同じ音を再提示", use_container_width=True):
            st.rerun()

    with st.expander("現在の項目内の反応履歴", expanded=True):
        hist = pd.DataFrame(trial["trials"])
        if hist.empty:
            st.write("まだ反応はありません。")
        else:
            st.dataframe(hist, use_container_width=True, height=180)

    with st.expander("検査上の注意", expanded=False):
        st.markdown(
            """
- 途中でOS音量、ブラウザ音量、ヘッドホン、部屋を変えないでください。  
- 迷う音は「聞こえない」として扱うと、スクリーニングとしては安全側になります。  
- app-dB はこのアプリ内の相対レベルであり、校正なしでは dB HL ではありません。  
- 1000Hz再測定差が大きい場合は、眠気、注意低下、教示理解、環境騒音、ヘッドホン装着を確認してください。  
"""
        )

if latest_run is not None:
    st.divider()
    st.subheader(f"最新結果: Run {latest_run['run_no']}")
    render_latest_summary_cards(latest_summary_df)

    st.subheader("神経心理検査用メモ")
    st.text_area("コピー用", value=str(latest_run.get("report_note", "")), height=180)

    st.subheader("オージオグラム風表示")
    latest_settings = latest_run.get("settings") or {}
    chart = build_audiogram_chart(
        latest_raw_df,
        min_level=int(latest_settings.get("min_level", DEFAULT_MIN_LEVEL)),
        max_level=int(latest_settings.get("max_level", DEFAULT_MAX_LEVEL)),
    )
    if chart is not None:
        st.altair_chart(chart, use_container_width=True)
        st.caption("横軸は周波数、縦軸は app-dB です。一般的な聴力図にならい、高い値ほど下に表示しています。")

    st.subheader("このrunの集計")
    st.dataframe(latest_summary_df, use_container_width=True)

    st.subheader("このrunの周波数別生データ")
    st.dataframe(latest_raw_df, use_container_width=True, height=240)

    run_log_df = log_df(run_no=int(latest_run["run_no"]))
    with st.expander("このrunの操作ログ", expanded=False):
        st.dataframe(run_log_df, use_container_width=True, height=240)

    st.subheader("ログ保存")
    latest_run_downloads()

history_df = build_history_dataframe()
if not history_df.empty:
    st.divider()
    st.subheader("実施履歴")
    st.dataframe(history_df, use_container_width=True, height=220)

session_log_df = log_df()
if not session_log_df.empty:
    st.divider()
    st.subheader("全sessionログ")
    st.dataframe(session_log_df, use_container_width=True, height=260)
