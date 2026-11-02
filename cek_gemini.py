import os
import re

import httpx


def safe_error(response, key):
    status = ""
    try:
        error = response.json().get("error", {})
        status = str(error.get("status", ""))
        message = str(error.get("message", "Tidak ada rincian error."))
    except (ValueError, AttributeError, TypeError):
        message = "Respons tidak berisi rincian JSON dari API."

    content_type = response.headers.get(
        "content-type", "unknown"
    ).split(";", 1)[0]

    text = (
        f"HTTP {response.status_code} {status} "
        f"({content_type}): {message}"
    )

    if key:
        text = text.replace(key, "[KEY DISENSOR]")

    text = re.sub(
        r"AIza[0-9A-Za-z_-]{15,}",
        "[KEY DISENSOR]",
        text,
    )
    text = re.sub(r"https?://\S+", "[URL]", text)

    return " ".join(text.split())[:480]


def fallback_models(data, current):
    choices = []

    for item in data.get("models", []):
        name = item.get("name", "").removeprefix("models/")

        if (
            "generateContent"
            not in item.get("supportedGenerationMethods", [])
            or not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", name)
            or "flash" not in name
            or name == current
            or any(
                word in name
                for word in (
                    "image", "tts", "audio", "live",
                    "native", "robot", "thinking",
                    "preview", "experimental", "-exp", "latest",
                )
            )
        ):
            continue

        choices.append(name)

    return sorted(
        set(choices),
        key=lambda name: (
            "lite" in name,
            tuple(int(n) for n in re.findall(r"\d+", name)),
            name,
        ),
        reverse=True,
    )[:2]


def main():
    key = os.environ.get("GEMINI_API_KEY", "").strip()

    current = os.environ.get(
        "MCM_AI_MODEL", "gemini-flash-latest"
    ).strip().removeprefix("models/")

    if not key:
        print("GEMINI_API_KEY belum ada di service ini.")
        return

    if (
        not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", current)
        or key in current
    ):
        print("MCM_AI_MODEL tidak valid.")
        return

    print(
        "API key ditemukan. Nilainya tidak ditampilkan.",
        flush=True,
    )

    with httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        headers={"x-goog-api-key": key},
        timeout=20,
    ) as client:
        response = client.get(
            "models", params={"pageSize": 1000}
        )

        print(
            "Daftar model:",
            "HTTP 200"
            if response.status_code == 200
            else safe_error(response, key),
            flush=True,
        )

        if response.status_code != 200:
            return

        models = [current] + fallback_models(
            response.json(), current
        )

        for model in models:
            print("Mencoba model:", model, flush=True)

            try:
                response = client.post(
                    f"models/{model}:generateContent",
                    json={
                        "contents": [{
                            "role": "user",
                            "parts": [{"text": "Balas hanya: OK"}],
                        }],
                        "generationConfig": {
                            "maxOutputTokens": 1024,
                        },
                    },
                    timeout=15,
                )

            except httpx.HTTPError as error:
                print(
                    model,
                    "->",
                    type(error).__name__,
                    "; lanjut ke model berikutnya.",
                    flush=True,
                )
                continue

            if response.status_code != 200:
                print(
                    model,
                    "->",
                    safe_error(response, key),
                    flush=True,
                )

                if response.status_code in (400, 401, 403, 429):
                    return

                continue

            candidates = response.json().get("candidates", [])
            parts = (
                candidates[0].get("content", {}).get("parts", [])
                if candidates
                else []
            )

            text = "".join(
                part["text"]
                for part in parts
                if isinstance(part.get("text"), str)
                and not part.get("thought")
            ).strip()

            if text:
                print(model, "-> BERHASIL", flush=True)
                print(
                    "Model yang berhasil:", model,
                    flush=True,
                )
                print(
                    "Jika berbeda dari model sekarang, isi "
                    "MCM_AI_MODEL dengan nama itu lalu deploy."
                )
                return

            print(
                model,
                "-> HTTP 200, tetapi tidak menghasilkan jawaban teks.",
                flush=True,
            )

        print("Belum ada model yang berhasil pada pemeriksaan ini.")


if __name__ == "__main__":
    try:
        main()
    except (
        httpx.HTTPError,
        ValueError,
        AttributeError,
        TypeError,
        KeyError,
    ) as error:
        print("Pemeriksaan terhenti:", type(error).__name__)
