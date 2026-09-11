"""
Message formatting helpers for prediction displays.
Clean, production-ready formatting without developer labels.
"""

import html
import io
from datetime import datetime

from zoneinfo import ZoneInfo

LAGOS_TZ = ZoneInfo("Africa/Lagos")


def _today_lagos_str() -> str:
    """Get today's date string in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")


def escape(text: str) -> str:
    """Escape HTML special characters in text."""
    return html.escape(str(text))


def _confidence_emoji(confidence: float) -> str:
    """Return emoji indicator based on confidence level."""
    if confidence >= 0.70:
        return "🟢"
    elif confidence >= 0.50:
        return "🟡"
    else:
        return "🟠"


def _confidence_tier_label(pred: dict) -> str:
    """Return the confidence tier label for a prediction."""
    tier = pred.get("confidence_tier", "")
    return {
        "High Confidence": "High",
        "Moderate Confidence": "Moderate",
        "Value Pick": "Value",
    }.get(tier, tier or "Value")


def format_single_prediction(pred: dict, index: int = None) -> str:
    """Format one prediction with compact match, selection, and timing copy."""
    prefix = f"{index}. " if index else ""
    confidence_pct = int(pred["confidence"] * 100)
    confidence_emoji = _confidence_emoji(pred["confidence"])
    tier_label = _confidence_tier_label(pred)

    msg = (
        f"{prefix}{pred['sport_icon']} <b>{escape(pred['match'])}</b>\n"
        f"   Selection: <b>{escape(pred['pick'])}</b>\n"
        f"   Odds: <b>{pred['odds']}</b> | Confidence: <b>{confidence_pct}%</b> {confidence_emoji} <b>{tier_label}</b>\n"
        f"   Kickoff: {escape(pred['match_time'])} · {escape(pred['league'])}\n"
    )
    return msg


def format_top_picks(predictions: list, count: int = 5) -> str:
    """Format top picks as an HTML message — clean and professional."""
    today_str = _today_lagos_str()
    picks = predictions[:count]

    msg = f"🏆 <b>Top Picks for {today_str}</b>\n\n"

    total_odds = 0
    for i, pred in enumerate(picks, 1):
        msg += format_single_prediction(pred, i)
        msg += "\n"
        total_odds += pred['odds']

    msg += f"<i>Combined Odds: <b>{total_odds:.2f}</b></i>"

    return msg


def format_all_picks_paginated(predictions: list, page: int = 1, per_page: int = 5) -> tuple:
    """
    Format predictions into paginated pages.

    Returns:
        Tuple of (message_text, total_pages)
    """
    today_str = _today_lagos_str()
    total_pages = max(1, (len(predictions) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_picks = predictions[start:end]

    msg = f"📋 <b>All Predictions — {today_str}</b>\n"
    msg += f"Page {page}/{total_pages} | Showing {len(page_picks)} of {len(predictions)} picks\n\n"

    for i, pred in enumerate(page_picks, start + 1):
        msg += format_single_prediction(pred, i)
        msg += "\n"

    # Add combined odds for ALL predictions (not just current page)
    total_odds = sum(p['odds'] for p in predictions)
    msg += f"<i>Combined Odds (All {len(predictions)} picks): <b>{total_odds:.2f}</b></i>"

    return msg, total_pages


def format_sport_filter_buttons(predictions: list) -> str:
    """Return sport filter message with active sports."""
    sports = {}
    for pred in predictions:
        sport = pred["sport"]
        if sport not in sports:
            sports[sport] = pred["sport_icon"]

    if not sports:
        return "⚠️ <b>No predictions available for today. Check back tomorrow morning!</b>"

    sport_list = ", ".join(f"{icon} {name}" for name, icon in sports.items())
    msg = f"⚽ <b>Select a sport to view today's predictions:</b>\n\n"
    msg += f"<i>Available: {sport_list}</i>"
    return msg


def format_file_attachment(content: str, filename: str = "predictions.txt") -> io.BytesIO:
    """Create a file attachment for oversized messages."""
    file_obj = io.BytesIO(content.encode("utf-8"))
    file_obj.name = filename
    return file_obj


def safe_message(text: str, max_length: int = 4000) -> tuple:
    """
    Safely prepare a message, falling back to file attachment if too long.

    Returns:
        Tuple of (text_or_file, is_file)
    """
    if len(text) <= max_length:
        return text, False
    return format_file_attachment(text, "predictions.txt"), True
