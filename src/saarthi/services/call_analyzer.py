"""
Call Analyzer — uses Gemini to analyze completed call transcripts.

Produces structured analysis: summary, topic classification,
risk level, and action items from a call transcript.

Repurposed from the original GeminiFastModel (legal fact extraction).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from google import genai
from google.genai import types

from saarthi.models.core import CallAnalysis
from saarthi.models.enums import CallTopic, RiskLevel
from saarthi.prompts.analysis import CALL_ANALYSIS_SYSTEM_PROMPT, build_analysis_prompt

logger = logging.getLogger(__name__)


class CallAnalyzer:
    """Gemini-based call transcript analyzer.

    Takes a completed call transcript and produces structured analysis
    including summary, topic classification, risk level, and action items.

    Usage:
        analyzer = CallAnalyzer(api_key="your_gemini_api_key")
        analysis = await analyzer.analyze(transcript_text)
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-2.0-flash",
        temperature: float = 0.2,
    ):
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name
        self._temperature = temperature

    async def analyze(self, transcript: str) -> CallAnalysis:
        """Analyze a call transcript and return structured analysis.

        Args:
            transcript: The full conversation transcript text.

        Returns:
            CallAnalysis with summary, topic, risk_level, and action_items.
        """
        if not transcript or len(transcript.strip()) < 10:
            logger.warning("Transcript too short for analysis")
            return CallAnalysis(
                summary="Call had minimal or no conversation.",
                topic=CallTopic.OTHER,
                risk_level=RiskLevel.LOW,
                action_items=[],
            )

        user_prompt = build_analysis_prompt(transcript)

        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model_name,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=CALL_ANALYSIS_SYSTEM_PROMPT,
                    temperature=self._temperature,
                    response_mime_type="application/json",
                ),
            )

            raw_json = response.text.strip()
            # Clean any markdown fences if present
            raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)

            parsed = json.loads(raw_json)
            return self._parse_analysis(parsed)

        except json.JSONDecodeError as e:
            logger.warning("Analysis returned invalid JSON: %s. Retrying...", e)
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self._model_name,
                    contents=(
                        f"{user_prompt}\n\n"
                        "IMPORTANT: Your previous response was not valid JSON. "
                        "Output ONLY a valid JSON object. No markdown, no explanation."
                    ),
                    config=types.GenerateContentConfig(
                        system_instruction=CALL_ANALYSIS_SYSTEM_PROMPT,
                        temperature=0.0,
                        response_mime_type="application/json",
                    ),
                )
                raw_json = response.text.strip()
                raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
                raw_json = re.sub(r"\s*```$", "", raw_json)
                parsed = json.loads(raw_json)
                return self._parse_analysis(parsed)
            except Exception as retry_e:
                logger.error("Analysis retry also failed: %s", retry_e)
                return self._fallback_analysis()

        except Exception as e:
            logger.error("Call analysis failed: %s", e)
            return self._fallback_analysis()

    @staticmethod
    def _parse_analysis(data: dict) -> CallAnalysis:
        """Parse a raw dict from Gemini into a validated CallAnalysis."""
        # Normalize topic
        topic_str = data.get("topic", "other")
        try:
            topic = CallTopic(topic_str.lower())
        except ValueError:
            topic = CallTopic.OTHER

        # Normalize risk level
        risk_str = data.get("risk_level", "low")
        try:
            risk_level = RiskLevel(risk_str.lower())
        except ValueError:
            risk_level = RiskLevel.LOW

        # Extract action items
        action_items = data.get("action_items", [])
        if not isinstance(action_items, list):
            action_items = []
        action_items = [str(item) for item in action_items if item]

        return CallAnalysis(
            summary=data.get("summary", ""),
            topic=topic,
            risk_level=risk_level,
            action_items=action_items[:5],  # Cap at 5
        )

    @staticmethod
    def _fallback_analysis() -> CallAnalysis:
        """Return a safe fallback analysis when Gemini fails."""
        return CallAnalysis(
            summary="Call analysis could not be completed automatically.",
            topic=CallTopic.OTHER,
            risk_level=RiskLevel.LOW,
            action_items=["Review call recording manually"],
        )
