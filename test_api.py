import os
import requests
import logging
import time

# Sett opp miljøvariablene lokalt for testing
PPLX_KEY = os.getenv("PPLX_API_KEY", "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")
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro")
HEADERS = {
    "Authorization": f"Bearer {PPLX_KEY}",
    "Content-Type": "application/json",
}

def ask_perplexity(company: str, person: str) -> tuple[str | None, str]:
    prompt = (
        f"Hva er MOBILNUMMERET (8 sifre) til {person} som jobber i {company} i Norge?\n"
        "Svar på to linjer:\n"
        "1) Kun åtte sammenhengende sifre (ingen +47, ingen tekst)\n"
        "2) Én URL-kilde\n"
    )
    try:
        start_time = time.time()
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=HEADERS,
            json={
                "model": PPLX_MODEL,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        js = r.json()
        text = js["choices"][0]["message"]["content"].strip()
        elapsed = time.time() - start_time
        logging.info("API call completed in %.2f seconds.", elapsed)
        
        # En enkel måte å trekke ut telefonnummer på (kun første linje)
        first_line = text.splitlines()[0] if text else ""
        # Her kan du implementere en enkel regex direkte om ønskelig
        number = first_line  # Testutskrift
        return number, ""
    except Exception as e:
        logging.error("API call failed: %s", e)
        return None, ""

if __name__ == "__main__":
    company = "Test Company"
    person = "Test Person"
    result, _ = ask_perplexity(company, person)
    print("Result:", result)
