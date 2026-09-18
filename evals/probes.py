"""Semantic probes: one decision point, many wordings.

Each probe is an ordinary simulated call in which one line, at one decision
point, is a wording generated fresh for this run. Everything else the simulated
person says is free. Pass or fail is decided by code, on what happened, so the
agent passes only if it understood the meaning, whatever the words.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.personas import (Outcome, Persona, _tomorrow, booked, callback_queued, first_offer_booked,
                            nothing_booked, on_offered_day_at, other_than_first_offer, results_not_shared,
                            results_shared)

# The rest of a cooperative call, around the probed line.
_CONFIRM = "Confirm it is you and that you have a few minutes."
_AGREE = "Listen to your results and agree to see a doctor."


@dataclass
class Point:
    id: str
    intent: str                 # what the probed line means, for the generator
    seeds: list[str]            # examples of the style wanted; never reused verbatim
    brief: str                  # the call, with {line} where the probed line goes
    outcomes: list[Outcome]
    answerer: str = "patient"
    keep: str = "none"          # facts every wording must keep
    tool: str = ""              # the tool this decision should lead to, if any


POINTS: list[Point] = [
    Point("identity-yes", "yes, I am the person you asked for",
          ["Yes.", "Speaking.", "That's me.", "Yeah, who's calling?"],
          "You are {name}. When the caller asks for you, reply with exactly: \"{line}\". Then say you have "
          "a few minutes. After hearing your results, say you will think about booking and say goodbye.",
          [results_shared, nothing_booked]),
    Point("identity-other", "I am not that person; I am someone else in their household",
          ["No, this is his wife.", "She's out, I'm her flatmate.", "No, I'm the brother."],
          "You answered the phone of {name}, but you are not them. When the caller asks for {first}, reply "
          "with exactly: \"{line}\". Then ask what the call is about, once. Then say goodbye.",
          [nothing_booked], answerer="other"),
    Point("busy", "I can't talk right now",
          ["Can't talk now.", "I'm in a meeting, sorry.", "Bad time, I'm driving."],
          "You are {name}. Confirm it is you. When asked if you have time, reply with exactly: \"{line}\". "
          "When asked when to call back, say tomorrow morning.",
          [nothing_booked, results_not_shared, callback_queued()], tool="request_callback"),
    Point("callback-time", "call me back tomorrow morning",
          ["Tomorrow morning is better.", "Try me tomorrow before noon.", "Morning tomorrow, please."],
          "You are {name}. Confirm it is you, then say you are busy right now. When asked when to call back, "
          "reply with exactly: \"{line}\".",
          [nothing_booked, callback_queued(lambda d: _tomorrow(d) and d.hour < 12, "tomorrow morning")],
          keep="tomorrow, morning", tool="request_callback"),
    Point("no-preference", "any time is fine, I have no preference",
          ["No.", "Whatever works.", "You decide.", "Any day's fine."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " When asked when suits you, reply with exactly: "
          "\"{line}\". Accept the first time you are offered.",
          [booked(lambda d: True, "any time")], tool="find_earliest_slot"),
    Point("evening", "I prefer the late part of the day",
          ["Evenings are better.", "After work, if possible.", "Late in the day."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " When asked when suits you, reply with exactly: "
          "\"{line}\". Name no day. Refuse any morning time. Accept the latest time you are offered.",
          [booked(lambda d: d.hour >= 15, "in the clinic's last hours, 3 to 5 PM")], tool="find_earliest_slot"),
    Point("morning", "I prefer mornings",
          ["Mornings.", "Early in the day works.", "Before lunch, please."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " When asked when suits you, reply with exactly: "
          "\"{line}\". Name no day. Accept the first morning time you are offered.",
          [booked(lambda d: d.hour < 12, "in the morning")], tool="find_earliest_slot"),
    Point("day-only", "Friday suits me, any time that day",
          ["Friday.", "Friday's good.", "I can do Friday."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " When asked when suits you, reply with exactly: "
          "\"{line}\". Accept the first Friday time you are offered.",
          [booked(lambda d: d.weekday() == 4, "on a Friday")], keep="Friday"),
    Point("time-no-day", "could it be at 12:30 instead",
          ["Can you do twelve thirty?", "What about 12:30?", "Half twelve?"],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " Ask for the earliest appointment. When a time is "
          "offered, reply with exactly: \"{line}\". Never mention a day. Accept once they confirm.",
          [on_offered_day_at(12, 30)], keep="12:30, no day"),
    Point("after-hours", "tomorrow at 7 in the evening",
          ["Tomorrow at 7 PM.", "Seven tomorrow evening?", "Tomorrow, around seven at night."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " When asked when suits you, reply with exactly: "
          "\"{line}\". Accept the latest time they can offer.",
          [booked(lambda d: 15 <= d.hour < 17, "within clinic hours, as late as possible")],
          keep="tomorrow, 7 PM"),
    Point("decline-offer", "that time does not work for me",
          ["No, that doesn't work.", "Hmm, not that one.", "Can't make that."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " Ask for the earliest appointment. When the first "
          "time is offered, reply with exactly: \"{line}\". Accept the next time offered.",
          [other_than_first_offer]),
    Point("accept-offer", "yes, book that time",
          ["Sure, book it.", "That works.", "Perfect."],
          "You are {name}. " + _CONFIRM + " " + _AGREE + " Ask for the earliest appointment. When a time is "
          "offered, reply with exactly: \"{line}\".",
          [first_offer_booked], tool="book_appointment"),
    Point("decline-visit", "I don't want an appointment",
          ["No thanks.", "I'll pass.", "Not interested in seeing anyone."],
          "You are {name}. " + _CONFIRM + " Listen to your results. When asked about seeing a doctor, reply "
          "with exactly: \"{line}\". If asked again, decline again, then say goodbye.",
          [nothing_booked]),
]

# What each probed line answers, so generated wordings fit the moment.
ASKED = {
    "identity-yes": "May I speak with the patient, please?",
    "identity-other": "May I speak with the patient, please?",
    "busy": "Do you have a few minutes to talk?",
    "callback-time": "When would be a good time to call you back?",
    "no-preference": "When would suit you for the appointment?",
    "evening": "When would suit you for the appointment?",
    "morning": "When would suit you for the appointment?",
    "day-only": "When would suit you for the appointment?",
    "time-no-day": "The earliest we have is tomorrow at 10:30. Does that work?",
    "after-hours": "When would suit you for the appointment?",
    "decline-offer": "The earliest we have is tomorrow at 10:30. Does that work?",
    "accept-offer": "The earliest we have is tomorrow at 10:30. Does that work?",
    "decline-visit": "Would you like to book a consultation with a doctor?",
}

BY_ID = {p.id: p for p in POINTS}


def case(point_id: str, line: str) -> Persona:
    """The probe for one wording, as a persona the runner already knows how to run."""
    p = BY_ID[point_id]
    return Persona(f"P:{p.id}", f"probe {p.id}", p.answerer,
                   p.brief.replace("{line}", line.replace("{", "{{").replace("}", "}}")),
                   valid_if="", outcomes=p.outcomes, suite="probes", point=p.id, phrase=line,
                   expect_tool=p.tool)
