import os
import json
import re
import requests
import logging

logger = logging.getLogger("mindpath.ai_service")

# All verified, active Gemini text-generation model IDs supporting generateContent and structured JSON output
SUPPORTED_GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-pro-latest"
]

def load_env_file(project_root=None):
    """
    Loads .env file from project root into os.environ.
    Uses python-dotenv if installed, otherwise uses built-in parser.
    Safe and idempotent: does not fail if .env does not exist.
    """
    root = project_root or os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(root, '.env')
    if not os.path.isfile(env_path):
        return False

    try:
        import dotenv
        dotenv.load_dotenv(env_path)
        return True
    except ImportError:
        pass

    try:
        with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        return True
    except Exception as e:
        print(f"Warning: Failed to load .env file: {e}")
        return False

# Load .env immediately upon module import
load_env_file()

def get_gemini_api_key():
    return os.environ.get("GEMINI_API_KEY", "").strip()

def sanitize_api_error(error_text: str, api_key: str = None) -> str:
    """
    Removes any API key, authorization tokens, or sensitive query strings from error messages.
    Guarantees the API key is never exposed.
    """
    if not error_text:
        return ""
    text = str(error_text)
    current_key = api_key or get_gemini_api_key()
    if current_key and len(current_key) >= 6:
        text = text.replace(current_key, "[REDACTED_API_KEY]")
    # Also strip patterns like key=... or api_key=...
    text = re.sub(r'(?:key|api_key|token|auth)=([A-Za-z0-9_\-]+)', r'key=[REDACTED]', text, flags=re.IGNORECASE)
    if len(text) > 300:
        text = text[:300] + "... (truncated)"
    return text.strip()

QUESTION_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question_text": {"type": "STRING"},
            "option_a": {"type": "STRING"},
            "option_b": {"type": "STRING"},
            "option_c": {"type": "STRING"},
            "option_d": {"type": "STRING"},
            "correct_option": {"type": "STRING"},
            "difficulty": {"type": "STRING"},
            "explanation": {"type": "STRING"}
        },
        "required": [
            "question_text", "option_a", "option_b", "option_c",
            "option_d", "correct_option", "difficulty", "explanation"
        ]
    }
}

def call_gemini_detailed(
    prompt: str, 
    max_tokens: int = 400, 
    timeout: int = 25, 
    json_mode: bool = False,
    response_schema: dict | None = None
) -> tuple[str | None, dict]:
    """
    Calls Gemini REST API using header authentication with model fallback sequence.
    Returns (response_text, diagnostics_dict).
    Guarantees API key is never exposed in URLs or error messages.
    """
    api_key = get_gemini_api_key()
    if not api_key:
        diag = {
            "success": False,
            "status_code": None,
            "error_type": "missing_key",
            "error_message": "GEMINI_API_KEY is not configured in .env or environment variables.",
            "model": None
        }
        return None, diag

    configured_model = os.environ.get("GEMINI_MODEL", "").strip()
    if configured_model:
        models_to_try = [configured_model] + [m for m in SUPPORTED_GEMINI_MODELS if m != configured_model]
    else:
        models_to_try = list(SUPPORTED_GEMINI_MODELS)

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2 if json_mode else 0.4,
            "maxOutputTokens": max_tokens
        }
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        if response_schema:
            payload["generationConfig"]["responseSchema"] = response_schema

    last_diag = {
        "success": False,
        "status_code": None,
        "error_type": "unknown_error",
        "error_message": "No model attempted.",
        "model": None
    }
    saw_quota_error = None

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates and "content" in candidates[0] and "parts" in candidates[0]["content"]:
                    text = candidates[0]["content"]["parts"][0]["text"].strip()
                    logger.info(f"Gemini API request succeeded using model '{model_name}'.")
                    print(f"[MindPath AI] Gemini API request succeeded using model '{model_name}'.")
                    return text, {
                        "success": True,
                        "status_code": 200,
                        "error_type": None,
                        "error_message": "",
                        "model": model_name
                    }
                else:
                    last_diag = {
                        "success": False,
                        "status_code": 200,
                        "error_type": "empty_response",
                        "error_message": "Response candidate was empty or blocked by safety filters.",
                        "model": model_name
                    }
                    logger.warning(f"Model '{model_name}' returned empty candidates. Trying next eligible model.")
                    continue

            raw_msg = ""
            try:
                raw_msg = res.json().get("error", {}).get("message", res.text)
            except Exception:
                raw_msg = res.text or f"HTTP Error {res.status_code}"

            clean_msg = sanitize_api_error(raw_msg, api_key)

            if res.status_code in (400, 403):
                # Fatal authentication error: API key is invalid across all models
                if any(w in raw_msg.upper() for w in ("API_KEY", "KEY_INVALID", "API KEY")):
                    logger.error(f"Gemini API authentication failed (HTTP {res.status_code}): {clean_msg}")
                    return None, {
                        "success": False,
                        "status_code": res.status_code,
                        "error_type": "invalid_key",
                        "error_message": clean_msg,
                        "model": model_name
                    }
                # Model-specific feature or permission error -> try next eligible model
                last_diag = {
                    "success": False,
                    "status_code": res.status_code,
                    "error_type": "permission_denied" if res.status_code == 403 else "invalid_request",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.warning(f"Model '{model_name}' rejected request (HTTP {res.status_code}): {clean_msg}. Trying next eligible model.")
                continue
            elif res.status_code == 429:
                last_diag = {
                    "success": False,
                    "status_code": 429,
                    "error_type": "quota_exceeded",
                    "error_message": clean_msg,
                    "model": model_name
                }
                saw_quota_error = last_diag
                logger.warning(f"Model '{model_name}' hit rate limit/quota (HTTP 429). Trying next eligible model.")
                continue
            elif res.status_code == 503:
                last_diag = {
                    "success": False,
                    "status_code": 503,
                    "error_type": "service_unavailable",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.warning(f"Model '{model_name}' service temporarily unavailable (HTTP 503). Trying next eligible model.")
                continue
            elif res.status_code in (500, 502, 504):
                last_diag = {
                    "success": False,
                    "status_code": res.status_code,
                    "error_type": "server_error",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.warning(f"Model '{model_name}' encountered temporary server error (HTTP {res.status_code}). Trying next eligible model.")
                continue
            elif res.status_code == 404:
                last_diag = {
                    "success": False,
                    "status_code": 404,
                    "error_type": "model_not_found",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.info(f"Model '{model_name}' not available or deprecated (HTTP 404). Trying next eligible model.")
                continue
            elif res.status_code == 408:
                last_diag = {
                    "success": False,
                    "status_code": 408,
                    "error_type": "request_timeout",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.warning(f"Model '{model_name}' request timed out (HTTP 408). Trying next eligible model.")
                continue
            else:
                last_diag = {
                    "success": False,
                    "status_code": res.status_code,
                    "error_type": "api_error",
                    "error_message": clean_msg,
                    "model": model_name
                }
                logger.warning(f"Model '{model_name}' returned HTTP {res.status_code}. Trying next eligible model.")
                continue

        except requests.exceptions.Timeout:
            last_diag = {
                "success": False,
                "status_code": None,
                "error_type": "network_timeout",
                "error_message": f"Network timeout after {timeout} seconds.",
                "model": model_name
            }
            logger.warning(f"Model '{model_name}' network timeout after {timeout}s. Trying next eligible model.")
            continue
        except requests.exceptions.ConnectionError:
            last_diag = {
                "success": False,
                "status_code": None,
                "error_type": "connection_error",
                "error_message": "Network connection error contacting Gemini API.",
                "model": model_name
            }
            logger.warning(f"Model '{model_name}' network connection error. Trying next eligible model.")
            continue
        except Exception as e:
            last_diag = {
                "success": False,
                "status_code": None,
                "error_type": "unexpected_error",
                "error_message": sanitize_api_error(str(e), api_key),
                "model": model_name
            }
            logger.warning(f"Model '{model_name}' unexpected exception: {e}. Trying next eligible model.")
            continue

    if saw_quota_error and last_diag.get("error_type") in ("model_not_found", "empty_response"):
        return None, saw_quota_error

    return None, last_diag

def call_gemini(prompt: str, max_tokens: int = 400, timeout: int = 15) -> str | None:
    """Calls Gemini REST API if key is present, otherwise returns None."""
    res, _ = call_gemini_detailed(prompt, max_tokens=max_tokens, timeout=timeout)
    return res

def get_ai_wrong_answer_explanation(question_text, correct_option_text, selected_option_text, base_explanation):
    """
    Returns an AI-crafted explanation addressing why the chosen option is incorrect 
    and how to remember the right answer.
    """
    prompt = f"""
    You are an AI computer science tutor for undergraduate engineering students.
    A student answered this question incorrectly.
    Question: {question_text}
    The student selected: {selected_option_text}
    The correct answer is: {correct_option_text}
    Explanation: {base_explanation}

    Provide a concise, friendly 2-3 sentence explanation:
    1. Explain why the selected answer is incorrect or a common misconception.
    2. Reinforce why the correct option is the right answer in simple terms.
    """
    ai_result = call_gemini(prompt)
    if ai_result:
        return ai_result

    # High-quality fallback rule engine for offline/no-key usage
    return (
        f"💡 Misconception Note: Choosing '{selected_option_text}' is a common slip-up when confusing definitions. "
        f"Remember that {correct_option_text} directly satisfies the requirement because: {base_explanation}"
    )

def get_personalized_study_roadmap(user_name, weak_topic_name, weak_subject, accuracy, speed_score):
    """
    Generates a 3-step personalized study plan based on the student's digital twin state.
    """
    if not weak_topic_name:
        return """
1. **Take the Initial Diagnostic**: Complete the 6-question comprehensive baseline assessment to calibrate your MindPath model.
2. **Review Topic Insights**: Identify your foundational strengths across C Programming, Data Structures, and DBMS.
3. **Targeted Practice**: Launch an adaptive quiz in your first weak area to strengthen your conceptual foundation.
""".strip()

    # Privacy-conscious prompt: do not include student sensitive PII
    prompt = f"""
    Generate a 3-step concise study recommendation for a computer science student.
    Weakest Topic: {weak_topic_name} in {weak_subject} (Current Accuracy: {accuracy}%, Answering Speed: {speed_score}%).
    Keep it actionable, CSE-focused, and bulleted.
    """
    ai_result = call_gemini(prompt)
    if ai_result:
        return ai_result

    # Dynamic fallback based on topic and metrics
    speed_advice = "Focus on conceptual clarity over speed right now." if speed_score < 60 else "Good speed; focus on eliminating minor edge-case mistakes."
    
    return f"""
1. **Targeted Core Revision**: Revise the fundamental axioms of {weak_topic_name} ({weak_subject}). Review standard textbook notes or quick concept cards for 15 minutes.
2. **Step-by-Step Problem Breakdown**: Solve 3-5 untimed practice questions specifically targeting common misconceptions in {weak_topic_name}. {speed_advice}
3. **Adaptive MindPath Retest**: Take an Adaptive Quiz on {weak_topic_name} to confirm your mastery transitions to 🟡 Average or 🟢 Strong.
""".strip()

def robust_parse_json_questions(raw_text: str) -> tuple[list | None, str | None]:
    """
    Multi-stage resilient JSON parser that handles:
    1. Standard JSON with markdown fences
    2. Substring extraction between '[' and ']'
    3. Repair of truncated JSON arrays by finding last valid complete object '}'
    4. Control character and unescaped newline normalization
    5. Regex extraction of individual valid question objects if top-level array is corrupted
    Returns (parsed_list_of_dicts, error_message).
    """
    if not raw_text:
        return None, "Empty response from AI"

    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    last_err = ""

    # Stage 1: Direct JSON parsing
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data, None
    except Exception as e1:
        last_err = str(e1)

    # Stage 2: Extract between first '[' and last ']'
    first_b = text.find('[')
    last_b = text.rfind(']')
    if first_b != -1 and last_b != -1 and last_b > first_b:
        try:
            data = json.loads(text[first_b:last_b + 1])
            if isinstance(data, list):
                return data, None
        except Exception as e2:
            last_err = str(e2)

    # Stage 3: Repair truncated array if stopped prematurely
    # (e.g. Unterminated string at token limit: slice up to last complete '}' and append ']')
    if first_b != -1:
        candidate_sub = text[first_b:]
        last_obj_close = candidate_sub.rfind('}')
        if last_obj_close != -1:
            repaired_array = candidate_sub[:last_obj_close + 1] + '\n]'
            try:
                data = json.loads(repaired_array)
                if isinstance(data, list) and len(data) > 0:
                    return data, None
            except Exception as e3:
                last_err = str(e3)

    # Stage 4: Sanitize control characters (unescaped raw newlines/tabs inside string literals)
    try:
        sanitized = re.sub(r'[\x00-\x1f]+', ' ', text)
        sb_first = sanitized.find('[')
        sb_last = sanitized.rfind(']')
        if sb_first != -1 and sb_last != -1 and sb_last > sb_first:
            data = json.loads(sanitized[sb_first:sb_last + 1])
            if isinstance(data, list):
                return data, None
    except Exception as e4:
        last_err = str(e4)

    # Stage 5: Regex extraction of individual valid question JSON objects
    extracted = []
    obj_pattern = re.compile(r'\{[^{}]*?"question_text"[^{}]*?\}', re.DOTALL)
    for m in obj_pattern.finditer(text):
        try:
            item = json.loads(m.group(0))
            if isinstance(item, dict) and "question_text" in item and "correct_option" in item:
                extracted.append(item)
        except Exception:
            continue

    if extracted:
        return extracted, None

    return None, last_err or "AI response could not be parsed as valid JSON."

def clean_json_response(raw_text: str) -> str:
    """Strips markdown code fences and whitespace for robust JSON parsing."""
    if not raw_text:
        return ""
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    # Extract JSON array substring between first [ and last ]
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        return match.group(0).strip()
    return text

def validate_question_dict(q: dict) -> dict | None:
    """
    Validates that a question dictionary adheres strictly to the required schema:
    - Non-empty question_text
    - 4 distinct options (option_a, option_b, option_c, option_d)
    - Unambiguous correct_option in {'A', 'B', 'C', 'D'}
    - difficulty in {'easy', 'medium', 'hard'}
    - Non-empty explanation
    """
    if not isinstance(q, dict):
        return None
    
    question_text = str(q.get("question_text", "")).strip()
    option_a = str(q.get("option_a", "")).strip()
    option_b = str(q.get("option_b", "")).strip()
    option_c = str(q.get("option_c", "")).strip()
    option_d = str(q.get("option_d", "")).strip()
    correct_option = str(q.get("correct_option", "")).strip().upper()
    difficulty = str(q.get("difficulty", "medium")).strip().lower()
    explanation = str(q.get("explanation", "")).strip()

    if not question_text or not option_a or not option_b or not option_c or not option_d:
        return None

    # Ensure 4 distinct options
    options_set = {option_a, option_b, option_c, option_d}
    if len(options_set) < 4:
        return None

    if correct_option not in ('A', 'B', 'C', 'D'):
        return None

    if difficulty not in ('easy', 'medium', 'hard'):
        difficulty = 'medium'

    if not explanation:
        explanation = f"Option {correct_option} is the correct answer for this question."

    return {
        "question_text": question_text,
        "option_a": option_a,
        "option_b": option_b,
        "option_c": option_c,
        "option_d": option_d,
        "correct_option": correct_option,
        "difficulty": difficulty,
        "explanation": explanation
    }

def generate_ai_questions(subject: str, topic: str, difficulty: str = "adaptive", count: int = 3, return_diagnostics: bool = False):
    """
    Generates multiple-choice questions for user-defined subject and topic using Gemini.
    Validates strictly against the question schema.
    If return_diagnostics=True, returns (validated_questions, diagnostics_dict).
    If return_diagnostics=False, returns validated_questions (or None).
    """
    if not subject or not topic:
        diag = {
            "success": False,
            "status_code": None,
            "error_type": "invalid_input",
            "error_message": "Subject and topic must not be empty.",
            "model": None
        }
        return (None, diag) if return_diagnostics else None

    diff_lower = (difficulty or "adaptive").lower().strip()
    if diff_lower in ("easy", "medium", "hard"):
        diff_instruction = f"All generated questions must have difficulty set to '{diff_lower}'."
    else:
        diff_instruction = "Provide a balanced distribution of 'easy', 'medium', and 'hard' difficulties for adaptive assessment."

    prompt = f"""You are an expert curriculum developer and academic examiner.
Generate exactly {count} high-quality, undergraduate-level multiple choice questions on the following topic:
Subject: {subject}
Topic: {topic}
{diff_instruction}

Requirements for each question:
1. Clear, precise, and unambiguous question statement.
2. Exactly 4 distinct plausible options: option_a, option_b, option_c, option_d.
3. Exactly one unambiguous correct answer specified as the option letter: "A", "B", "C", or "D".
4. Difficulty must strictly be one of: "easy", "medium", "hard".
5. A concise explanation (strictly 1 sentence, max 30 words) detailing why the correct answer is right.
6. Formatting Rule: Never use unescaped double quotes inside strings. Use single quotes for any code symbols or terms (e.g. 'main()', 'printf()').
7. Ensure all JSON string literals are properly closed and valid.
8. Keep question statements, options, and explanations concise, precise, and direct to conserve tokens.

Respond ONLY with a valid raw JSON array of objects (no markdown wrapping, no text outside JSON).
JSON schema:
[
  {{
    "question_text": "...",
    "option_a": "...",
    "option_b": "...",
    "option_c": "...",
    "option_d": "...",
    "correct_option": "A",
    "difficulty": "medium",
    "explanation": "..."
  }}
]"""

    # Headroom prevents premature truncation while avoiding wasteful token reservations
    max_tokens = max(1600, count * 450)

    raw_response, last_diag = call_gemini_detailed(
        prompt,
        max_tokens=max_tokens,
        timeout=25,
        json_mode=True,
        response_schema=QUESTION_RESPONSE_SCHEMA
    )

    # API failure across all candidate models: return immediately without retrying
    if not raw_response:
        return (None, last_diag) if return_diagnostics else None

    # Parse using multi-stage repair pipeline
    data, parse_err = robust_parse_json_questions(raw_response)
    if not data or not isinstance(data, list):
        last_diag.update({
            "success": False,
            "error_type": "malformed_json",
            "error_message": f"AI response could not be parsed as valid JSON: {str(parse_err or 'invalid format')[:100]}"
        })
        return (None, last_diag) if return_diagnostics else None

    validated = []
    for item in data:
        v = validate_question_dict(item)
        if v:
            if diff_lower in ("easy", "medium", "hard"):
                v["difficulty"] = diff_lower
            validated.append(v)

    if not validated:
        last_diag.update({
            "success": False,
            "error_type": "invalid_schema",
            "error_message": "Generated questions did not pass the 4-option validation schema."
        })
        return (None, last_diag) if return_diagnostics else None

    last_diag.update({
        "success": True,
        "error_type": None,
        "error_message": ""
    })
    succeeded_model = last_diag.get("model", "unknown")
    logger.info(
        f"Successfully generated {len(validated)} questions for '{subject} - {topic}' "
        f"(difficulty: {diff_lower}) using model '{succeeded_model}'."
    )
    print(
        f"[MindPath AI] Successfully generated {len(validated)} questions for '{subject} - {topic}' "
        f"(difficulty: {diff_lower}) using model '{succeeded_model}'."
    )
    return (validated, last_diag) if return_diagnostics else validated
