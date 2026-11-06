# SPDX-FileCopyrightText: 2026 Shane Califano <scalifano4740@floridapoly.edu>
#
# SPDX-License-Identifier: MIT

"""ReqClarify

A clarification algorithm.

Team: NEXUS Lab
Team Members: Shane Califano, Mohamed Sylla, William Blaine, Karim Elish
Main Contact: kelish@floridapoly.edu
"""

from __future__ import annotations

import collections
import heapq
import itertools
import math
import re
from typing import Any

from clarify.baselines.base import ClarificationAlgorithmBase
from clarify.env import ClarificationEnvironment

# clarification prompt: decide if we need to ask one focused question
SystemClarificationPrompt = '''
You are a software requirements analyst. Do NOT generate code.

Identify whether the requirement contains an unresolved ambiguity that could affect the correctness of the implementation.
Prefer questions that meet these criteria when possible:
- Criticality: addresses a real blocker.
- Search space: not trivially guessable.
- Leakage: does not ask about hidden tests or pure implementation details.
- Atomicity: targets one main issue.
- Objectivity: can be answered clearly.

Rate the need for clarification:
0 = Sufficiently specified.
1 = Minor but plausible behavioral ambiguity.
2 = Ambiguity likely to affect correctness.
3 = Critical missing information or contradiction.

Generate at most ONE short clarification question (≤ 25 words).
Focus on the single most important behavioral choice (boundaries, ties, ordering, inclusion, empty inputs, numerical meaning, contradictions).
If the requirement is already clear under standard conventions, return NEED: 0 and QUESTION: NONE.

Return strictly in the exact plaintext format below. No markdown, no extra text:

NEED: <0|1|2|3>
ISSUE: <Short Description>
INTERPRETATION_A: <Behavior A>
INTERPRETATION_B: <Behavior B>
QUESTION: <Question or NONE>
'''

# coding prompt: generate the function, treat clarification as law, force correct name
SystemCodePrompt = '''
You are an expert Python programmer solving MBPP-style tasks.

Implement the requested function according to the original requirement and the user's clarification.
The user's clarification is authoritative for the issue that was asked about. Follow it exactly.
Do not invent unrelated requirements.

Strict rules:
1. Always include an explicit return on every path.
2. Do not use overly strict isinstance(x, int) checks that reject floats unless the prompt explicitly requires integers only. Prefer direct comparisons or isinstance(x, (int, float)).
3. Main collection first when a function takes a collection and a target/sublist/element, unless clarification or the prompt says otherwise.
4. When extracting words, strip punctuation or use re.findall(r"\\b\\w+\\b", text).
5. Use only ASCII. No comments, no explanations, no tests.
6. The function MUST be named exactly as specified. Do not invent or rename the function.

Additional rules for common MBPP pitfalls:
- For prime/not-prime checks, treat 0, 1, and negatives as not prime.
- For sequence checks (octagonal, triangular, etc.), implement the exact mathematical test or n-th term as the prompt requires; do not return a boolean if a numeric term is expected.
- For substring or word counting, be precise about overlapping vs. non-overlapping and whether the total count or a filtered count is wanted.
- When counting or filtering by type (e.g., positive integers), use direct comparisons or isinstance(x, (int, float)) and exclude bools; do not reject valid floats unless the prompt explicitly requires pure ints.
- Always match the expected return type implied by the prompt and clarification (boolean, int, list, string, etc.).

Return ONLY the Python implementation.
'''

# audit prompt: check and fix common mistakes, keep the correct function name
SystemAuditPrompt = '''
You are a strict software correctness auditor.

Check the candidate implementation against the original requirement and any user clarification.
Repair these common errors if present:
- Missing return
- Wrong parameter order
- Overly strict type checks (isinstance int instead of allowing float)
- Punctuation attached to words
- Wrong return type (boolean vs value, list vs single item, etc.)
- Missed boundary cases (empty, zero, negative, nested)
- Incorrect function name

Also repair:
- Incorrect prime/sequence definitions on 0, 1, negatives.
- Wrong return type (boolean when a number or list is expected, or vice versa).
- Off-by-one or overlapping errors in counting/substring functions.
- Type-check failures on mixed numeric inputs.

If the code is already correct, return it unchanged.
Return ONLY the final Python implementation (ASCII only, no comments or explanations).
The function MUST keep the exact required name.
'''

# smoke prompt: just check the function runs and returns something
SystemSmokePrompt = '''
You are a Python test runner.
Write 2 simple function call lines that only check the function runs and does not return None.
Use assert func(...) is not None. Do not assert specific values.
Return ONLY the 2 lines of code.
'''


class ReqClarifyClarification(ClarificationAlgorithmBase):

    def extract_code(self, raw_text: str) -> str:
        # pull python out of markdown fences if present, force ascii
        match = re.search(r"```(?:python)?\s*(.*?)```", raw_text, re.DOTALL | re.IGNORECASE)
        code = match.group(1).strip() if match else raw_text.replace("```python", "").replace("```", "").strip()
        return code.encode("ascii", "ignore").decode("ascii")
    
    def run(self, env: ClarificationEnvironment, problem: dict[str, Any]) -> str:
        # set up the three conversation threads
        clarification_messages = [{"role": "system", "content": SystemClarificationPrompt}]
        programming_messages = [{"role": "system", "content": SystemCodePrompt}]
        audit_messages = [{"role": "system", "content": SystemAuditPrompt}]

        user_prompt = problem["prompt"]
        entry_point = problem.get("entry_point", "")

        # give everyone the original requirement
        clarification_messages.append({"role": "user", "content": user_prompt})
        programming_messages.append({"role": "user", "content": "Original Prompt : " + user_prompt})
        audit_messages.append({"role": "user", "content": "Original Prompt : " + user_prompt})

        # force the exact function name so tests don't fail with nameerror
        if entry_point:
            name_instruction = f"The function MUST be named exactly `{entry_point}`. Do not use any other name."
            programming_messages.append({"role": "user", "content": name_instruction})
            audit_messages.append({"role": "user", "content": name_instruction})

        # ask at most one clarification question
        max_questions = 1
        questions_asked = 0
        clarification_attempts = []

        while questions_asked < max_questions and env.can_ask():
            should_clarify = env.llm(clarification_messages)
            should_clarify = should_clarify.replace("```text", "").replace("```", "").strip()
            clarification_messages.append({"role": "assistant", "content": should_clarify})

            clarification_results = self.parse_clarification(should_clarify, threshold=0)

            if (
                clarification_results["need"]
                and clarification_results["question"]
                and clarification_results["question"].upper() != "NONE"
                and env.can_ask()
            ):
                questions_asked += 1
                clarification_response = env.ask_human(clarification_results["question"])
                clarification_messages.append({"role": "user", "content": clarification_response})
                clarification_attempts.append((clarification_results, clarification_response))
            else:
                break

        # pass the full analysis into coding and audit
        for question_dict, question_response in clarification_attempts:
            full_context = (
                f"ISSUE: {question_dict.get('issue', '')}\n"
                f"INTERPRETATION_A: {question_dict.get('interpretation_a', '')}\n"
                f"INTERPRETATION_B: {question_dict.get('interpretation_b', '')}\n"
                f"Clarification Question: {question_dict['question']}\n"
                f"User Response: {question_response}"
            )
            programming_messages.append({"role": "user", "content": full_context})
            audit_messages.append({"role": "user", "content": full_context})

        # generate the first version of the code
        raw_code = env.llm(programming_messages)
        pre_audit_code = self.extract_code(raw_code)
        code = pre_audit_code

        # audit it
        audit_messages.append({"role": "user", "content": f"Candidate Implementation:\n```python\n{code}\n```"})
        audited_raw = env.llm(audit_messages)
        audited_code = self.extract_code(audited_raw)

        # keep the original if the audit returns something unusable
        if audited_code and len(audited_code.strip()) > 10:
            code = audited_code
        else:
            code = pre_audit_code

        audit_messages.append({"role": "assistant", "content": code})

        # light smoke tests just to make sure it runs
        smoke_messages = [
            {"role": "system", "content": SystemSmokePrompt},
            {"role": "user", "content": f"Original Prompt : {user_prompt}\n\nCandidate Code:\n{code}"},
        ]
        smoke_tests = self.extract_code(env.llm(smoke_messages))

        test_block = "\n".join(problem.get("test_list", []))
        combined_tests = (test_block + "\n" + smoke_tests).strip()

        # try a few times if the code crashes or fails tests
        max_audit_attempts = 3
        exec_globals = {
            "math": math,
            "re": re,
            "heapq": heapq,
            "collections": collections,
            "itertools": itertools,
        }

        for _ in range(max_audit_attempts):
            clean_code = self.extract_code(code)
            try:
                compile(clean_code, "<string>", "exec")
                if combined_tests:
                    full_script = clean_code + "\n\n" + combined_tests
                    exec(full_script, dict(exec_globals))
                code = clean_code
                break
            except Exception as e:
                error_message = (
                    f"The code failed during execution with the following error:\n"
                    f"Exception: {str(e)}\n\n"
                    f"Please fix the implementation and return ONLY the corrected Python code."
                )
                audit_messages.append({"role": "user", "content": error_message})
                code = self.extract_code(env.llm(audit_messages))
                audit_messages.append({"role": "assistant", "content": code})

        return self.extract_code(code)

    def parse_clarification(self, clarification: str, threshold: int) -> dict[str, Any]:
        # pull the structured fields out of the clarification response
        outputs = {
            "need": False,
            "issue": "",
            "interpretation_a": "",
            "interpretation_b": "",
            "question": "",
        }

        need_match = re.search(r"NEED:\s*(\d+)", clarification, re.IGNORECASE)
        if need_match:
            outputs["need"] = int(need_match.group(1)) > threshold

        issue_match = re.search(r"ISSUE:\s*(.*?)(?=INTERPRETATION_A:|$)", clarification, re.IGNORECASE | re.DOTALL)
        if issue_match:
            outputs["issue"] = issue_match.group(1).strip()

        int_a_match = re.search(r"INTERPRETATION_A:\s*(.*?)(?=INTERPRETATION_B:|$)", clarification, re.IGNORECASE | re.DOTALL)
        if int_a_match:
            outputs["interpretation_a"] = int_a_match.group(1).strip()

        int_b_match = re.search(r"INTERPRETATION_B:\s*(.*?)(?=QUESTION:|$)", clarification, re.IGNORECASE | re.DOTALL)
        if int_b_match:
            outputs["interpretation_b"] = int_b_match.group(1).strip()

        question_match = re.search(r"QUESTION:\s*(.*)", clarification, re.IGNORECASE | re.DOTALL)
        if question_match:
            outputs["question"] = question_match.group(1).strip()

        return outputs