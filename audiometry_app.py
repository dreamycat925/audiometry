"""
簡易聴力スクリーニング Streamlit アプリ

目的:
- 神経心理検査の前に、聴覚入力の問題がありそうかを短時間で確認する。
- 非校正ヘッドホンでは dB HL ではなく app-dB として扱う。

注意:
- 診断用の純音聴力検査、障害認定、補聴器適合には使用しない。
- dB HL として出す場合は、同一PC・同一ヘッドホン・同一音量で院内校正プロファイルを作成してから使う。
"""

from __future__ import annotations

import io
import json
import math
import wave
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

# -----------------------------
# 基本設定
# -----------------------------
FS = 44_100
DURATION_SEC = 0.75
RAMP_SEC = 0.05
DEFAULT_START_LEVEL = 40
DEFAULT_MIN_LEVEL = 0
DEFAULT_MAX_LEVEL = 80
DEFAULT_MAX_DBFS = -8.0  # MAX_LEVEL app-dB のときのデジタルピークレベル
APP_DB_REFERENCE_MAX_LEVEL = DEFAULT_MAX_LEVEL  # app-dB の音量基準は常に固定する

FREQ_SEQUENCE = [1000, 2000, 4000, 500, 1000]
FREQ_LABELS = ["1000Hz", "2000Hz", "4000Hz", "500Hz", "1000Hz再測定"]
CORE_FREQS = [500, 1000, 2000, 4000]
EAR_OPTIONS = {
    "右→左": ["右", "左"],
    "左→右": ["左", "右"],
}

# 画面表示の揺れを減らすための列順
RAW_COLUMNS = [
    "item_no",
    "ear",
    "freq_hz",
    "measure_type",
    "threshold_app_db",
    "censored",
    "trials",
    "completed_at",
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
# 検査順序・状態管理
# -----------------------------
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


def reset_app() -> None:
    for key in [
        "started",
        "order",
        "idx",
        "results",
        "trial",
        "settings",
        "calibration",
    ]:
        if key in st.session_state:
            del st.session_state[key]


def start_test(settings: Dict[str, Any], calibration: Optional[Dict[str, Any]]) -> None:
    st.session_state.started = True
    st.session_state.order = build_order(settings["ear_order"])
    st.session_state.idx = 0
    st.session_state.results = []
    st.session_state.settings = settings
    st.session_state.calibration = calibration
    st.session_state.trial = new_trial(
        settings["start_level"], settings["min_level"], settings["max_level"]
    )


def current_item() -> Dict[str, Any]:
    return st.session_state.order[st.session_state.idx]


def record_threshold(threshold: int, censored: bool = False) -> None:
    item = current_item()
    trial = st.session_state.trial
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
    st.session_state.results.append(result)
    st.session_state.idx += 1
    settings = st.session_state.settings
    st.session_state.trial = new_trial(
        settings["start_level"], settings["min_level"], settings["max_level"]
    )


def respond(heard: bool) -> None:
    """
    速い上昇法。

    1) 開始レベルで聞こえれば10dBずつ下げ、聞こえなくなったら5dBずつ上げる。
    2) 開始レベルで聞こえなければ10dBずつ上げ、初回反応後に直前の不反応+5dBから確認する。
    3) 上昇系列で最初に聞こえたレベルを閾値とする。
    """
    trial = st.session_state.trial
    level = int(trial["level"])
    phase = trial["phase"]
    min_level = int(trial["min_level"])
    max_level = int(trial["max_level"])

    trial["trials"].append({"level": level, "heard": bool(heard), "phase": phase})

    # すでに最小レベルで聞こえる場合は床値として記録
    if heard and level <= min_level and phase in {"init", "down"}:
        record_threshold(min_level, censored=False)
        return

    # 最大レベルでも聞こえない場合は右打ち切りとして記録
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
    except Exception as exc:  # noqa: BLE001 - UIでエラー表示するため広く受ける
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
def as_raw_dataframe() -> pd.DataFrame:
    df = pd.DataFrame(st.session_state.results)
    if df.empty:
        return pd.DataFrame(columns=RAW_COLUMNS)
    return df[RAW_COLUMNS]


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
    ears = ["右", "左"]
    for ear in ears:
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

        # 旧来4分法: (500 + 2*1000 + 2000) / 4
        if all(freq in app_values for freq in [500, 1000, 2000]):
            row["旧来4分法_app_dB"] = round(
                (app_values[500] + 2 * app_values[1000] + app_values[2000]) / 4.0, 1
            )
        else:
            row["旧来4分法_app_dB"] = math.nan

        # 4周波数平均: (500 + 1000 + 2000 + 4000) / 4
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
                row["信頼性メモ"] = "1000Hz再測定差が10dB以上：信頼性低下の可能性"
            elif diff > 5:
                row["信頼性メモ"] = "1000Hz再測定差が5dB超：注意"
            else:
                row["信頼性メモ"] = ""
        else:
            row["1000Hz再測定差_app_dB"] = math.nan
            row["信頼性メモ"] = ""

        if censored_freqs:
            freq_text = ", ".join(f"{freq}Hz" for freq in censored_freqs)
            row["打ち切りメモ"] = f"最大提示レベルでも反応なし: {freq_text}"
        else:
            row["打ち切りメモ"] = ""

        rows.append(row)

    return pd.DataFrame(rows)


def generate_neuropsych_note(summary_df: pd.DataFrame, calibration: Optional[Dict[str, Any]]) -> str:
    if summary_df.empty:
        return "結果がありません。"

    value_col = "旧来4分法_est_dBHL" if calibration and "旧来4分法_est_dBHL" in summary_df else "旧来4分法_app_dB"
    unit = "推定dB HL" if value_col.endswith("est_dBHL") else "app-dB（非校正）"

    notes: List[str] = []
    notes.append("【神経心理検査用メモ】")
    notes.append(f"本結果は {unit} による参考値である。")

    ear_values: Dict[str, float] = {}
    for _, row in summary_df.iterrows():
        ear = str(row["耳"])
        value = row.get(value_col, math.nan)
        if pd.notna(value):
            ear_values[ear] = float(value)

            # 診断分類ではなく、神経心理検査上の注意フラグとして控えめに表現する
            if value >= 40:
                notes.append(
                    f"{ear}耳の旧来4分法は {value:.1f} {unit} で、聴覚提示課題の解釈に注意を要する。"
                )
            elif value >= 30:
                notes.append(
                    f"{ear}耳の旧来4分法は {value:.1f} {unit} で、軽度の聴覚入力低下の可能性に注意する。"
                )

        reliability = str(row.get("信頼性メモ", ""))
        if reliability:
            notes.append(f"{ear}耳: {reliability}")

        censored = str(row.get("打ち切りメモ", ""))
        if censored:
            notes.append(f"{ear}耳: {censored}")

    if "右" in ear_values and "左" in ear_values:
        diff = abs(ear_values["右"] - ear_values["左"])
        if diff >= 15:
            notes.append(
                f"左右の旧来4分法差は {diff:.1f} {unit} で、左右差の可能性がある。"
            )

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
) -> str:
    lines: List[str] = []
    lines.append("簡易聴力スクリーニング結果")
    lines.append("=" * 32)
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
        lines.append("校正プロファイル: なし。表示値はdB HLではなく app-dB。")

    lines.append("")
    lines.append("集計")
    lines.append("-" * 32)
    if not summary_df.empty:
        lines.append(summary_df.to_string(index=False))
    else:
        lines.append("集計結果なし")

    lines.append("")
    lines.append(generate_neuropsych_note(summary_df, calibration))

    lines.append("")
    lines.append("注記")
    lines.append("- 本検査は非校正または院内校正のPC/ブラウザ音源およびヘッドホンによる簡易聴覚スクリーニングである。")
    lines.append("- 標準純音聴力検査の代替ではなく、診断、障害認定、補聴器適合には使用しない。")
    lines.append("- 神経心理検査における聴覚入力条件の確認を目的とした参考値として扱う。")
    return "\n".join(lines)


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="簡易聴力スクリーニング", page_icon="👂", layout="centered")

st.title("簡易聴力スクリーニング")
st.caption("神経心理検査前の聴覚入力チェック用。非校正では dB HL ではなく app-dB として扱います。")

if "started" not in st.session_state:
    st.session_state.started = False

with st.sidebar:
    st.header("検査設定")
    if st.session_state.started:
        st.info("検査中は設定を固定しています。変更する場合はリセットしてください。")
        if st.button("検査をリセット", type="secondary"):
            reset_app()
            st.rerun()
    else:
        test_id = st.text_input("検査ID / 患者IDなど", value="")
        headphone = st.text_input("ヘッドホン名", value="")
        device_note = st.text_input("PC / ブラウザ / OS音量", value="OS音量100%、ブラウザ音量100%")
        environment_note = st.text_area("環境メモ", value="静かな個室。ヘッドホン左右確認済み。", height=80)
        ear_order = st.radio("検査順", list(EAR_OPTIONS.keys()), horizontal=True)
        start_level = st.number_input("開始レベル app-dB", min_value=0, max_value=90, value=DEFAULT_START_LEVEL, step=5)
        min_level = st.number_input("最小レベル app-dB", min_value=0, max_value=40, value=DEFAULT_MIN_LEVEL, step=5)
        max_level = st.number_input("最大レベル app-dB", min_value=40, max_value=100, value=DEFAULT_MAX_LEVEL, step=5)
        autoplay = st.checkbox("音を自動再生する", value=True)

        st.divider()
        use_calibration = st.checkbox("院内校正プロファイルを使う", value=False)
        calibration_upload = None
        calibration_data = None
        calibration_error = None
        if use_calibration:
            calibration_upload = st.file_uploader("校正JSONをアップロード", type=["json"])
            calibration_data, calibration_error = parse_calibration(calibration_upload)
            if calibration_error:
                st.error(calibration_error)
            elif calibration_data:
                st.success(f"校正プロファイル: {calibration_data.get('profile_name', '名称なし')}")

# 開始前画面
if not st.session_state.started:
    st.warning(
        "このアプリは簡易スクリーニング用です。診断用の純音聴力検査、障害認定、補聴器適合には使用しないでください。"
    )

    st.subheader("実施前チェック")
    st.markdown(
        """
1. できるだけ静かな部屋で実施する。  
2. 有線ヘッドホンを推奨し、PC・OS・ブラウザの音量を固定する。  
3. 患者さんには「音が聞こえたらすぐに押してください。迷った場合は聞こえないを押してください」と説明する。  
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

    # sidebarで定義した変数を安全に参照
    settings_ready = True
    if "max_level" in locals() and "min_level" in locals() and max_level <= min_level:
        st.error("最大レベルは最小レベルより大きくしてください。")
        settings_ready = False
    if "start_level" in locals() and "min_level" in locals() and "max_level" in locals():
        if not (min_level <= start_level <= max_level):
            st.error("開始レベルは最小〜最大レベルの範囲内にしてください。")
            settings_ready = False
    if "use_calibration" in locals() and use_calibration and calibration_data is None:
        st.info("校正プロファイルを使う場合は、有効なJSONをアップロードしてください。")
        settings_ready = False

    start_clicked = st.button("検査開始", type="primary", disabled=not settings_ready)
    if start_clicked:
        settings = {
            "test_id": test_id,
            "headphone": headphone,
            "device_note": device_note,
            "environment_note": environment_note,
            "ear_order": ear_order,
            "start_level": int(start_level),
            "min_level": int(min_level),
            "max_level": int(max_level),
            "autoplay": bool(autoplay),
            "use_calibration": bool(use_calibration),
        }
        start_test(settings, calibration_data)
        st.rerun()

    st.stop()

# 検査中・終了画面
settings = st.session_state.settings
calibration = st.session_state.calibration
order = st.session_state.order
idx = st.session_state.idx

if idx >= len(order):
    st.success("検査終了")
    raw_df = as_raw_dataframe()
    summary_df = summarize(raw_df, calibration)

    st.subheader("集計")
    st.dataframe(summary_df, use_container_width=True)

    st.subheader("神経心理検査用メモ")
    report_note = generate_neuropsych_note(summary_df, calibration)
    st.text_area("コピー用", value=report_note, height=180)

    st.subheader("周波数別の生データ")
    st.dataframe(raw_df, use_container_width=True)

    text_report = make_text_report(raw_df, summary_df, settings, calibration)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "集計CSV",
            data=summary_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="hearing_summary.csv",
            mime="text/csv",
        )
    with col2:
        st.download_button(
            "生データCSV",
            data=raw_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="hearing_raw.csv",
            mime="text/csv",
        )
    with col3:
        st.download_button(
            "レポートTXT",
            data=text_report.encode("utf-8-sig"),
            file_name="hearing_report.txt",
            mime="text/plain",
        )

    st.warning(
        "本結果は簡易スクリーニングです。異常値、左右差、再測定差大、聴覚症状がある場合は標準聴力検査を検討してください。"
    )

    if st.button("新しい検査を開始"):
        reset_app()
        st.rerun()
    st.stop()

# 検査項目画面
item = current_item()
trial = st.session_state.trial
level = int(trial["level"])
progress = idx / len(order)

st.progress(progress)
st.write(f"進行状況: {idx + 1} / {len(order)}")

st.subheader(f"{item['ear']}耳　{item['label']}")
st.metric("提示レベル", f"{level} app-dB")

if calibration:
    est_level = apply_calibration(level, calibration, item["ear"], int(item["freq_hz"]))
    st.caption(f"この周波数・耳では、提示レベルの目安は {est_level:.1f} 推定dB HL です。")

wav_bytes = make_tone_wav(
    freq_hz=int(item["freq_hz"]),
    level_app_db=level,
    ear=str(item["ear"]),
    max_level=APP_DB_REFERENCE_MAX_LEVEL,
)
st.audio(wav_bytes, format="audio/wav", autoplay=bool(settings.get("autoplay", True)))
st.caption("自動再生されない場合は、プレイヤーの再生ボタンを押してください。")

col1, col2, col3 = st.columns(3)
with col1:
    if st.button("聞こえた", type="primary", use_container_width=True):
        respond(True)
        st.rerun()
with col2:
    if st.button("聞こえない", use_container_width=True):
        respond(False)
        st.rerun()
with col3:
    if st.button("同じ音を再提示", use_container_width=True):
        st.rerun()

with st.expander("現在の項目内の反応履歴"):
    hist = pd.DataFrame(trial["trials"])
    if hist.empty:
        st.write("まだ反応はありません。")
    else:
        st.dataframe(hist, use_container_width=True)

with st.expander("検査上の注意"):
    st.markdown(
        """
- 途中でOS音量、ブラウザ音量、ヘッドホン、部屋を変えないでください。  
- 迷う音は「聞こえない」として扱うと、スクリーニングとしては安全側になります。  
- app-dBはこのアプリ内の相対レベルであり、校正なしではdB HLではありません。  
- 1000Hz再測定差が大きい場合は、眠気、注意低下、教示理解、環境騒音、ヘッドホン装着を確認してください。  
"""
    )
