from __future__ import annotations
from typing import Any

from clarify.env import ClarificationEnvironment, TooManyQuestionException
from clarify.baselines.base import ClarificationAlgorithmBase
from clarify.runtime import _validate_and_parse_evalplus_result


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
        ClarificationMessages = [{"role": "system", "content": SystemClarificationPrompt}];
        ProgrammingMessages = [{"role": "system", "content": SystemCodePrompt}];
        AuditMessages = [{"role": "system", "content": SystemAuditPrompt}];

        UserPrompt = Problem["prompt"];
        ClarificationMessages += [{"role": "user", "content": UserPrompt}];
        ProgrammingMessages += [{"role": "system", "content": "Original Prompt : " + UserPrompt}];
        AuditMessages += [{"role": "system", "content": "Original Prompt : " + UserPrompt}];
        
        # Begin clarification loop. Technically the original plan in the document said to only ask one question, but I figure an implementation like this
        # gives us more room to change our plans if necessary.
        MaxQuestions = 100;
        QuestionsAsked = 0;
        while True:
            ClarificationAttempts = [];
            ShouldClarify = Env.llm(ClarificationMessages);
            ClarificationMessages = [{"role": "assistant", "content": ShouldClarify}];

            #After this, we should have the aforementioned clarification prompt (NEED, ISSUE, stuff) in the ShouldClarify variable. Parsing should be done next.

            ClarificationResults = Self.ParseClarification(ShouldClarify, Threshold=1);

            # After parsing, check if we both can ask a question and have a question to ask. Also if the questions asked already does not exceed our desired maximum.
            if ClarificationResults["Need"] and can_ask() and QuestionsAsked < MaxQuestions:
                QuestionsAsked += 1;
                ClarificationResponse = ask_human(ClarificationResults["Question"]);
                ClarificationMessages = [{"role": "user", "content": ClarificationResponse}];
                ClarificationAttempts.append((ClarificationResults, ClarificationResponse));
            else:
                break;

            # Second check to save some money and maybe time.
            if QuestionsAsked >= MaxQuestions or not can_ask():
                break;
        
        # Set up coding AI for loop.
        for Question in ClarificationAttempts:
            QuestionDict = Question[0];
            QuestionResponse = Question[1];
            Message = f"Clarification Question : {QuestionDict["Question"]}\nUser Response : {QuestionResponse}";
            ProgrammingMessages += [{"role": "system", "content": Message}];
            AuditMessages += [{"role": "system", "content": Message}];


        Audit = True;
        # Begin coding/auditing.
        Code = Env.llm(ProgrammingMessages);
        if Audit:
            # Note here that we do actually have exec_code which could be useful. For now I'm just going to pass the code to the auditor AI.
            Message = f"Current Code :\n\n`{Code}`";
            AuditMessages += [{"role": "system", "content": Message}];
            Code = Env.llm(AuditMessages);

        return Code;




    def ParseClarification(Self, Clarification, Threshold):
        # We could also try splitting these by line instead of just by space, which may be better, depending on the output we're receiving.
        Outputs = dict();
        ListVer = Clarification.split(" "); # Split into list.
        LowerList = Clarification.lower().split(" "); # Split into list but lowercase, just in case the AI model is not following our specific instructions.
        NeedPos = LowerList.index("need:") + 1; # Position of the NEED indicator.
        Need = ListVer[NeedPos]; # The NEED value. (Should be 0-3).

        Outputs["Need"] = int(Need) > Threshold; # Boolean for whether or not a question is necessary.

        IssueSearch = "ISSUE:"
        IssuePos = Clarification.lower().find(IssueSearch.lower());

        InterpretationSearchA = "INTERPRETATION_A:"
        InterpretationAPos = Clarification.lower().find(InterpretationSearchA.lower());

        InterpretationSearchB = "INTERPRETATION_B:"
        InterpretationBPos = Clarification.lower().find(InterpretationSearchB.lower());

        QuestionSearch = "QUESTION:" # The QUESTION search term.
        QuestionPos = Clarification.lower().find(QuestionSearch.lower());

        Issue = Clarification[IssuePos+IssueSearch.size():InterpretationAPos].strip(); # Get the issue without whitespace.

        InterpretationA = Clarification[InterpretationAPos+InterpretationAPos.size():InterpretationBPos].strip();

        InterpretationB = Clarification[InterpretationBPos+InterpretationBPos.size():QuestionPos].strip();

        Question = Clarification[QuestionPos+QuestionSearch.size():];

        Outputs["Issue"] = Issue;
        Outputs["InterpretationA"] = InterpretationA;
        Outputs["InterpretationB"] = InterpretationB;
        Outputs["Question"] = Question.strip();

        return Outputs

        