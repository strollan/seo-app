import os
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def google_search(keyword, location="United States", page=1, num=10, gl="us", hl="en"):
    query = keyword
    use_live_serp = os.getenv("USE_LIVE_SERP", "false").strip().lower() == "true"

    if not use_live_serp:
        raise RuntimeError(
            "Live Serper calls are disabled. Set USE_LIVE_SERP=true to enable."
        )

    serper_api_key = (os.getenv("SERPER_API_KEY") or "").strip()

    if not serper_api_key:
        raise ValueError("Missing SERPER_API_KEY in .env file")

    url = "https://google.serper.dev/search"

    payload = {
        "q": query,
        "location": location,
        "page": page,
        "num": num,
        "gl": gl,
        "hl": hl,
        "num": num,
    }

    headers = {
        "X-API-KEY": serper_api_key,
        "Content-Type": "application/json",
    }

    response = requests.post(url, json=payload, headers=headers)

    if not response.ok:
        print("Serper error status:", response.status_code)
        print("Serper error response:", response.text)
        print("SERPER_API_KEY loaded:", f"{serper_api_key[:6]}...{serper_api_key[-4:]}")
        print("SERPER_API_KEY length:", len(serper_api_key))
        response.raise_for_status()

    return response.json()


def get_organic_results(data):
    return data.get("organic", [])


def get_people_also_ask(data):
    return data.get("peopleAlsoAsk", [])


def get_related_searches(data):
    return data.get("relatedSearches", [])
