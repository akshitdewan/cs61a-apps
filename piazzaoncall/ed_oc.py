import hashlib

import datetime
import numpy as np
import csv
import pytz
import re
from dateutil.parser import parse

from common.db import connect_db
from common.rpc.auth import ed_course_id, read_spreadsheet, post_slack_message
from ed import network

STAFF = read_spreadsheet(
    url="https://docs.google.com/spreadsheets/d/1rhZEVryWVhMWiEyHZWMDhk_zgQ_4eg_RJevq2K3nVno/",
    sheet_name="piazza-bot",
)
headers = STAFF[0]
STAFF = STAFF[1:]

STAFF_LST = []
for row in STAFF:
    for _ in range(int(row[headers.index("Weight")])):
        STAFF_LST.append(row)

TIMEZONE = pytz.timezone("America/Los_Angeles")

NIGHT_START, NIGHT_END = datetime.time(0, 30), datetime.time(15, 30)
MORNING = datetime.time(7, 0)


with connect_db() as db:
    db(
        """CREATE TABLE IF NOT EXISTS status (
    last_sent INTEGER
)"""
    )
    ret = db("SELECT last_sent FROM status").fetchone()
    if ret is None:
        db("INSERT INTO STATUS (last_sent) VALUES (0)")


def send(message, course):
    post_slack_message(course=course, message=message, purpose="ed-reminder")


class Main:
    def __init__(self):
        with connect_db() as db:
            self.last_sent = datetime.datetime.fromtimestamp(
                db("SELECT last_sent FROM status").fetchone()[0], TIMEZONE
            )
        self.roncall = re.compile(r"oncall:\s*(\S+)")  # '\[oncall: .*\]'

        self.urgent_threshold = 3
        self.url_starter = f"https://edstem.org/us/courses/{ed_course_id()}/discussion/"

    def send_message(self):
        """Sends a message for all unresolved posts or followups made after
        self.ignore_before. Uses weights column from input CSV to proportionally
        allocate staff members to questions"""
        message, high_priority = "", ""
        for post in network.list_unresolved():
            post_num = post.get("number")
            post_id = post.get("id")

            assigned = self.oncall(post)
            if assigned == "ignore":
                print(
                    f"{datetime.datetime.today().date()}: @{post_num} is marked as ignore"
                )
                continue
            elif assigned:
                str = ""
                for email in assigned:
                    str += f"<!{email}> "
                str += f"your assigned post (<{self.url_starter}{post_id}|@{post_num}>) needs help!\n"
                message += str
                continue

            if not post.get("is_answered", False) and post.get("unresolved_count", 0):
                staff, priority = self.select_staff(post)
                str = f"<!{staff}> please help <{self.url_starter}{post_id}|@{post_num}>\n"
                if priority:
                    high_priority += str
                else:
                    message += str

        if message:
            starter = (
                "Good morning! Here are today's Ed assignments. You will receive a daily reminder "
                "about your unresolved Ed posts. *If you do not know how to answer your post(s), "
                "post in #general.*\n\n "
            )
            # print(starter + message)
            send(starter + message, course="cs61a")
        if high_priority:
            starter = (
                f"These messages have been unanswered for {self.urgent_threshold} days. "
                "*If you were assigned one of these posts, please reply to this message after you have resolved "
                "it.*\n\n "
            )
            send(starter + high_priority, course="cs61a")

    def oncall(self, post):
        """Returns email of staff member on call if specified in body of instructor Ed post using syntax
        oncall: <bConnected Username> (berkeley email without @berkeley.edu). oncall: IGNORE can be used to tell
        the bot to exclude the post from oncall."""
        if post.get("user", {}).get("course_role", "student") not in ["admin"]:
            return None
        text = post.get("document")
        usernames = [u.lower() for u in re.findall(self.roncall, text)]
        if usernames:
            if "ignore" in usernames:
                return "ignore"
            else:
                return [u + "@berkeley.edu" for u in usernames]
        return None

    def select_staff(self, post):
        """Selects staff member(s) for the post. Randomly assigns a staff member to answer the post and any
        unresolved followups (one staff member for the post itself, one additional staff member for each unresolved
        followup. Returns a tuple two elements:
            1. Staff member email
            2. Boolean indicating priority (True=urgent)
        Urgent post @61 would return (email, True)"""
        return self.pick_staff(post.get("id")), self.is_urgent(post)

    def pick_staff(self, post_id):
        """Given a post ID, assign a staff member and return staff member's email. Staff members selected from
        STAFF dataframe (imported from staff_roster.csv)"""
        post_hash = int(hashlib.sha224((str(post_id)).encode("utf-8")).hexdigest(), 16)
        staff_index = post_hash % len(STAFF_LST)
        return STAFF_LST[staff_index][headers.index("Email")]

    def is_urgent(self, post):
        """Returns a boolean indicating whether the input post or followup is urgent. For a post to be urgent,
        it must be made after self.ignore_before and more than self.urgent_threshold business days old. For a followup
        to be urgent, it must be made after self.ignore_before and its NEWEST reply must be more than
        self.urgent_threshold business days old. Notes are never urgent, but their followups can be."""
        kind = post.get("type")
        if kind == "note":
            return False
        if kind == "question":
            newest = parse(post.get("created_at", "2001-08-27T04:53:21Z")).date()
        elif kind == "followup":
            children = post.get("comments", [])
            if children:
                newest = parse(
                    children[-1].get("created_at", "2001-08-27T04:53:21Z")
                ).date()
            else:
                newest = parse(post.get("created_at", "2001-08-27T04:53:21Z")).date()
        else:
            print("Unknown post type: " + post.get("type"))
            newest = parse("2001-08-27T04:53:21Z").date()
        today = datetime.datetime.utcnow().date()
        return np.busday_count(newest, today) >= self.urgent_threshold

    def run(self):
        tod_in_ca = datetime.datetime.now(tz=TIMEZONE).time()
        day = datetime.datetime.now(tz=TIMEZONE).weekday()

        if day >= 5:
            print(f"{tod_in_ca}: Skipping — weekend: {day}")
        elif tod_in_ca < MORNING:
            print(f"{tod_in_ca}: Skipping — before morning: {tod_in_ca}")
        elif self.last_sent.date() == datetime.datetime.now(tz=TIMEZONE).date():
            print(
                f"{tod_in_ca}: Skipping — already sent today: {self.last_sent.date()}"
            )
        else:
            self.send_message()
            print(f"{tod_in_ca}: SENT MESSAGE FOR {datetime.datetime.today().date()}")
            with connect_db() as db:
                db(
                    "UPDATE status SET last_sent=(%s)",
                    [datetime.datetime.today().timestamp()],
                )


if __name__ == "__main__":
    run = Main()
    run.send_message()
