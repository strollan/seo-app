import os
from dotenv import load_dotenv
from pathlib import Path
import json
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

openai_api_key = os.getenv("OPENAI_API_KEY")
if not openai_api_key:
    raise RuntimeError("OPENAI_API_KEY is missing. Add it to .env.")

client = OpenAI(api_key=openai_api_key)


def run_seo_agent(analysis_data: dict) -> dict:
    prompt = f"""
You are an expert SEO strategist.

Return ONLY valid JSON in this exact format:

{{
  "summary": "",
  "top_actions": [
    {{
      "action": "",
      "reason": "",
      "priority": "high"
    }}
  ]
}}

Analyze this SEO data:

{json.dumps(analysis_data, indent=2)}
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are a senior SEO strategist."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {
            "summary": "Agent returned non-JSON output.",
            "top_actions": [
                {
                    "action": "Review agent output formatting",
                    "reason": content,
                    "priority": "medium"
                }
            ]
        }
