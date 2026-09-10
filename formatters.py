"""
Message formatting helpers for prediction displays.
Clean, production-ready formatting without developer labels.
"""

import html
import io
from datetime import datetime, timezone
from typing import List, Dict


def escape(text: str) -> str:
    """Escape HTML special characters in text."""
    return html.escape(str(text))


def _confidence_emoji(confidence: float) -> str:
    """Return emoji indicator based on confidence level."""
    if confidence >= 0.80:
        return "🟢"
    elif confidence >= 0.65:
        return "🟡"
    else:
        return "🟠"


def format_single_prediction(pred: dict, index: int = None) -> str:
    """Format a single prediction as HTML — clean and professional."""
    prefix = f"{index}. " if index else ""
    confidence_pct = int(pred["confidence"] * 100)
    confidence_emoji = _confidence_emoji(pred["confidence"])

    msg = (
        f"{prefix}{pred['sport_icon']} <b>{escape(pred['league'])}</b>\n"
        f"   🏟️ {escape(pred['match'])}\n"
        f"   📊 Market: {escape(pred['market_label'])} → <b>{escape(pred['pick'])}</b>\n"
        f"   💰 Odds: <b>{pred['odds']}</b> | 🎯 Confidence: <b>{confidence_pct}%</b> {confidence_emoji}\n"
        f"   🕐 {escape(pred['match_time'])}\n"
    )
    return msg


def format_top_picks(predictions: List[dict], count: int = 5) -> str:
    """Format top picks as an HTML message — clean and professional."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    picks = predictions[:count]

    msg = f"🏆 <b>Top Picks for {today_str}</b>\n\n"

    for i, pred in enumerate(picks, 1):
        msg += format_single_prediction(pred, i)
        msg += "\n"

    msg += f"<i>Showing top {len(picks)} of {len(predictions)} predictions</i>\n"
    msg += "<i>Picks refresh daily at 00:00 UTC</i>"

    return msg


def format_all_picks_paginated(predictions: List[dict], page: int = 1, per_page: int = 5) -> tuple:
    """
    Format predictions into paginated pages.

    Returns:
        Tuple of (message_text, total_pages)
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    return msg, total_pages


def format_sport_filter_buttons(predictions: List[dict]) -> str:
    """Return sport filter message with active sports."""
    sports = {}
    for pred in predictions:
        sport = pred["sport"]
        if sport not in sports:
            sports[sport] = pred["sport_icon"]

    if not sports:
        return "⚠️ <b>No predictions available for today. Check back tomorrow morning!</b>"

    sport_list = ", ".join([f"{icon} {name}" for name, icon in sports.items()])
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
