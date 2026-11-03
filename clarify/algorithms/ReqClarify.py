from __future__ import annotations
from typing import Any
from clarify.env import ClarificationEnvironment, TooManyQuestionException
from clarify.baselines.base import ClarificationAlgorithmBase
from clarify.runtime import _validate_and_parse_evalplus_result

import re

# Sample Quickstart code, close to as seen in the README file.

'''
class QuickStartClarification(ClarificationAlgorithmBase):
    def run(Self, Env: ClarificationEnvironment, Problem: dict[str, Any]) -> str:
        Messages = [{"role": "user", "content": Problem["prompt"]}];

        Response = Env.llm(Messages);

        Messages += [{"role": "assistant", "content": Response}];

        while True:
            try:
                return _validate_and_parse_evalplus_result(Response);
            except ValueError:
                try:
                    Answer = Env.ask_human(Response);
                    Messages += [{"role": "user", "content": Answer}];
                    Response = Env.llm(Messages);
                    Messages += [{"role": "assistant", "content": Response}];
                except TooManyQuestionException:
                    return Response;
'''


SystemClarificationPrompt =  '''
You are a software requirements analyst. Do NOT generate code.

Analyze the programming requirement and determine whether it contains an unresolved behavioral choice that could affect the correctness of the implementation.

A valid clarification issue must satisfy these conditions:

1. Two reasonable interpretations are possible.
2. The interpretations can lead to different observable program behavior.
3. The correct interpretation cannot be safely determined from the requirement.
4. One concise clarification question can resolve the issue.

Focus on behavioral issues such as boundary conditions, duplicates, ties, ordering, inclusion/exclusion, empty or special inputs, output semantics, numerical interpretation, missing constraints, or contradictions.

Do NOT ask about implementation strategy, algorithms, data structures, performance, code style, or hidden tests.

Rate the need for clarification:
0 = Requirement is sufficiently specified.
1 = Minor but plausible behavioral ambiguity.
2 = Ambiguity likely to affect correctness.
3 = Critical missing information or contradiction.

Generate at most ONE clarification question.
The question must be atomic, objective, concise, and no longer than 30 words.

Return:

NEED: <0|1|2|3>
ISSUE: <Short Description>
INTERPRETATION_A: <Behavior A>
INTERPRETATION_B: <Behavior B>
QUESTION: <Question or NONE>

'''

SystemCodePrompt = '''
You are a Python programmer.

Implement the requested function according to the original requirement and, when provided, the user's clarification.

The user's clarification is authoritative for the issue that was asked about.

Do not invent unrelated requirements.

Before generating the implementation, silently determine the behavioral contract of the function and verify that the implementation satisfies it.

Return ONLY the Python implementation.
Do not provide explanations or tests.
'''

SystemAuditPrompt = '''
You are a strict software correctness auditor.

Check whether the candidate implementation satisfies:

1. The original requirement.
2. The user's clarification, if provided.
3. The intended behavioral choice.
4. Explicit boundary conditions.
5. Requirements unrelated to the clarification.

Look specifically for a plausible input for which the implementation would violate the intended behavior.

If there is a semantic error, repair the implementation.

If the implementation is already correct, leave it unchanged.

Return ONLY the final Python implementation.
Do not provide explanations or tests.
'''

class ReqClarifyClarification(ClarificationAlgorithmBase):
    def run(Self, Env: ClarificationEnvironment, Problem: dict[str, Any]) -> str:
        # Create all three of the different "AIs" although it's just different message histories.
        ClarificationMessages = [{"role": "system", "content": SystemClarificationPrompt}]
        ProgrammingMessages = [{"role": "system", "content": SystemCodePrompt}]
        AuditMessages = [{"role": "system", "content": SystemAuditPrompt}]

        UserPrompt = Problem["prompt"]
        ClarificationMessages += [{"role": "user", "content": UserPrompt}]
        ProgrammingMessages += [{"role": "user", "content": "Original Prompt : " + UserPrompt}]
        AuditMessages += [{"role": "user", "content": "Original Prompt : " + UserPrompt}]
        
        # Begin clarification loop. Technically the original plan in the document said to only ask one question, but I figure an implementation like this
        # gives us more room to change our plans if necessary.
        MaxQuestions = 100
        QuestionsAsked = 0
        ClarificationAttempts = []

        while True:
            ShouldClarify = Env.llm(ClarificationMessages);
            ClarificationMessages += [{"role": "assistant", "content": ShouldClarify}]

            #After this, we should have the aforementioned clarification prompt (NEED, ISSUE, stuff) in the ShouldClarify variable. Parsing should be done next.

            ClarificationResults = Self.ParseClarification(ShouldClarify, Threshold=1)

            # After parsing, check if we both can ask a question and have a question to ask. Also if the questions asked already does not exceed our desired maximum.
            if ClarificationResults["Need"] and Env.can_ask() and QuestionsAsked < MaxQuestions:
                QuestionsAsked += 1
                ClarificationResponse = Env.ask_human(ClarificationResults["Question"])
                ClarificationMessages += [{"role": "user", "content": ClarificationResponse}]
                ClarificationAttempts.append((ClarificationResults, ClarificationResponse))
            else:
                break;

            # Second check to save some money and maybe time.
            if QuestionsAsked >= MaxQuestions or not Env.can_ask():
                break;
        
        # Set up coding AI for loop.
        for Question in ClarificationAttempts:
            QuestionDict = Question[0]
            QuestionResponse = Question[1]
            Message = f"Clarification Question : {QuestionDict["Question"]}\nUser Response : {QuestionResponse}"
            ProgrammingMessages += [{"role": "user", "content": Message}]
            AuditMessages += [{"role": "user", "content": Message}]


        Audit = True
        MaxAuditAttempts = 5
        Code = Env.llm(ProgrammingMessages)
        AuditMessages += [{"role": "assistant", "content": Code}]

        if Audit:
            for _ in range(MaxAuditAttempts):
                CleanCode = Code.replace("```python", "").replace("```", "").strip()
                
                try:
                    TestBlock = "\n".join(Problem.get("test_list", []))
                    FullScript = CleanCode + "\n\n" + TestBlock
                    
                    exec(FullScript, {})

                    Code = CleanCode
                    break
                
                except Exception as e:
                    ErrorMessage = f"The code failed during testing with the following error:\n{type(e).__name__}: {str(e)}\n\nPlease fix this error and return ONLY the corrected Python code."
                    AuditMessages += [{"role": "user", "content": ErrorMessage}]
                    Code = Env.llm(AuditMessages)
                    AuditMessages += [{"role": "assistant", "content": Code}]
        
        return Code.replace("```python", "").replace("```", "").strip()




    def ParseClarification(Self, Clarification, Threshold):
        Outputs = {
            "Need": False,
            "Issue": "",
            "InterpretationA": "",
            "InterpretationB": "",
            "Question": ""
        }
        
        # Safely extract NEED value
        need_match = re.search(r'NEED:\s*(\d+)', Clarification, re.IGNORECASE)
        if need_match:
            Outputs["Need"] = int(need_match.group(1)) > Threshold
            
        # Safely extract text blocks
        issue_match = re.search(r'ISSUE:\s*(.*?)(?=INTERPRETATION_A:|$)', Clarification, re.IGNORECASE | re.DOTALL)
        if issue_match: Outputs["Issue"] = issue_match.group(1).strip()
        
        int_a_match = re.search(r'INTERPRETATION_A:\s*(.*?)(?=INTERPRETATION_B:|$)', Clarification, re.IGNORECASE | re.DOTALL)
        if int_a_match: Outputs["InterpretationA"] = int_a_match.group(1).strip()
        
        int_b_match = re.search(r'INTERPRETATION_B:\s*(.*?)(?=QUESTION:|$)', Clarification, re.IGNORECASE | re.DOTALL)
        if int_b_match: Outputs["InterpretationB"] = int_b_match.group(1).strip()
        
        question_match = re.search(r'QUESTION:\s*(.*)', Clarification, re.IGNORECASE | re.DOTALL)
        if question_match: Outputs["Question"] = question_match.group(1).strip()
        
        return Outputs

        