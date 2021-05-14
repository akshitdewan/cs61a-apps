import io
import random
import signal
import sys
from collections import defaultdict
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from json import dump, dumps, load, loads
from multiprocessing.pool import ThreadPool
from os import urandom
from random import randint, randrange
from textwrap import indent
from typing import Optional

import click
from tqdm import tqdm

from examtool.api.database import get_exam, get_roster, get_submissions
from examtool.api.extract_questions import extract_questions
from examtool.api.scramble import scramble
from examtool.api.utils import rel_path

sys.path.append(rel_path("../../../"))
from common.rpc.code import create_code_shortlink

sys.path.append("scheme")
import scheme


@dataclass
class Test:
    stmt: str
    out: str = ""
    result: Optional[str] = None


def parse_doctests(s: str):
    out = []
    case = None
    for line in s.strip().split("\n"):
        line = line.strip()
        if not line:
            if case:
                out.append(case)
            case = None
            continue

        if line.startswith(">>> "):
            if case:
                out.append(case)
            case = Test(stmt=line[4:])
        else:
            if not case:
                continue

            if line.startswith("..."):
                case.stmt += "\n" + line[4:]
            else:
                case.out += "\n" + line

    if case:
        out.append(case)

    return out


def depth(line):
    return len(line) - len(line.strip())


def indent_fixer(value):
    value = value.replace("\t", " " * 4)
    return value


def run(code, globs, *, is_scm=False, is_stmt=False, only_err=False, timeout=2):
    did_timeout = False

    def timeout_handler(*_):
        nonlocal did_timeout
        did_timeout = True
        raise Exception("TIMEOUT")

    signal.signal(signal.SIGALRM, timeout_handler)
    err = None

    f = io.StringIO()
    with redirect_stdout(f):
        signal.alarm(timeout)
        try:
            if is_scm:
                buffer = scheme.Buffer(scheme.tokenize_lines(code.split("\n")))
                while buffer.current():
                    ret = scheme.scheme_eval(scheme.scheme_read(buffer), globs)
                    if ret is not None:
                        print(ret)
            else:
                if not is_stmt:
                    try:
                        ret = eval(code, globs)
                        if ret is not None:
                            print(repr(ret))
                    except SyntaxError:
                        is_stmt = True
                if is_stmt:
                    exec(code, globs)
        except Exception as e:
            print(e)
            err = str(e)

    signal.alarm(0)

    if did_timeout:
        print("Timeout")

    if only_err:
        return err

    return f.getvalue()


@click.command()
def autograde(fetch=True, num_threads=1):
    from examtool.cli.DO_NOT_UPLOAD_FINAL_DOCTESTS import doctests, templates

    EXAM = "cs61a-sp21-final-alt-3"

    with open(f"{EXAM}_submissions.json", "w" if fetch else "r") as f:
        if fetch:
            submissions = {k: v for k, v in get_submissions(exam=EXAM)}
            dump(submissions, f)
        else:
            submissions = load(f)

    exam = get_exam(exam=EXAM)

    out = {}

    try:

        def grade_student(email):
            submission = submissions.get(email, {})
            exam_copy = loads(dumps(exam))
            questions = {
                q["id"]: q
                for q in extract_questions(scramble(email, exam_copy, keep_data=True))
            }
            out[email] = {}

            for template_name, template in templates.items():
                subs = {}

                def sub(s):
                    for k, v in subs.items():
                        s = s.replace(k, v)
                    return s

                data = {}

                for key, value in submission.items():
                    if key not in questions:
                        continue
                    if not isinstance(value, str):
                        continue
                    if key not in template:
                        continue
                    subs.update(questions[key]["substitutions"])

                    value = indent_fixer(value)

                    value = value.strip()

                    if value.startswith("`") and value.endswith("`"):
                        value = value[1:-1]

                    if value.endswith(":"):
                        value = value[:-1]

                    if key in [
                        "IDDJWBBKBVOXJFDZOXJRXOYFNGVCQVTL",
                        "WOWQBXUAFPBQTAGRPFRCNUWUDEULSLKA",
                        "SNQWEXTNYLCEAVSZELXGDUIMVYZDYARJ",
                        "IBADPNGWKULHOOELPHOQJDEVWXQTBEUG",
                        "KDVKLPFWMRZEDYUMGNDDJAHKQGMKBRNX",
                        "FIKHGOUTZCVWZCOPRDKRMWXTWTCYBFAV",
                    ]:
                        value = value.removeprefix("return ")

                    if key in [
                        "UUZALBQMQRZPFJRMTUNTQJQWXRBKWDDA",
                        "DZIKGHCCBIHDFBICFLMMERHLUPXRVMGS",
                        "FSICNXZFGZBIQQZTHIIQUKCANRKDUPEG",
                    ]:
                        value = value.removeprefix("if ")

                    if key in ["RZWVONEHLKCURGTNXPAGCLSMLKIQAPSQ"]:
                        value = value.removeprefix("for ")

                    if key in ["RTSIKWOYCJMTDPEOAWYTZRSJEOZHNWHM"]:
                        value = value.removeprefix("yield ")

                    if key in ["IWMTWSLDZXWWQECONOGQDJDSSEXGRDYQ"]:
                        value = value.removeprefix("for ")
                        value = value.removeprefix("k ")
                        value = value.removeprefix("in ")

                    if key in ["WAIZFSEOLDEUNJUJTXNYXJFVZMVOYKSS"]:
                        value = value.removeprefix("if ")
                        value = value.removeprefix("(if ")

                    value = value.strip()

                    for level in range(0, 12, 4):
                        data["_" * level + key] = indent(value, " " * level)

                soln = sub(template.format_map(defaultdict(str, **data)))
                globs = {}

                is_scm = isinstance(doctests[template_name], str)

                if isinstance(doctests[template_name], str):
                    # scm
                    globs = scheme.create_global_frame()
                    status = run(soln, globs, is_stmt=True, only_err=True, is_scm=True)
                    globs.define("__run_all_doctests", True)
                    test_raw = run(sub(doctests[template_name]), globs, is_scm=True)
                    test_split = [
                        eval(x)
                        for x in test_raw.strip().split("DOCTEST: ")
                        if x.strip()
                    ]
                    tests = [
                        Test(
                            stmt=x["code"][0],
                            out=x["expected"].replace("\n", r"\n"),
                        )
                        for x in test_split
                    ]
                    results = [x["raw"].replace("\n", r"\n") for x in test_split]
                else:
                    status = run(soln, globs, is_stmt=True, only_err=True)
                    tests = [replace(test) for test in doctests[template_name]]

                for i, test in enumerate(tests):
                    test.stmt = sub(test.stmt)
                    test.out = sub(test.out)
                    if status is None:
                        if is_scm:
                            result = results[i]
                        else:
                            result = run(test.stmt, globs).strip()
                        if result != test.out.strip():
                            test.result = (
                                f"FAILED: Expected {test.out.strip()}, got {result}"
                            )
                        else:
                            test.result = f"SUCCESS: Got {result}"
                        test.result = test.result.replace("\n", r"\n")

                    # print(status, tests)

                if is_scm:
                    content = soln + sub(doctests[template_name])
                else:

                    def render(i, test):
                        stmt_lines = test.stmt.strip().split("\n")
                        stmt_disp = "\n... ".join(stmt_lines)
                        return f"# Case {i}\n>>> {stmt_disp}" + (
                            f"\n{test.out.strip()}" if test.out else ""
                        )

                    cases = "\n".join(render(i, test) for i, test in enumerate(tests))

                    content = (
                        soln
                        + "\n\n"
                        + f"""
def placeholder(): 
    pass

placeholder.__doc__ = '''
{cases}
                '''
                """.lstrip()
                    )

                url = "temp"

                random.seed(urandom(32))

                url = create_code_shortlink(
                    name=f"{template_name}.{'scm' if is_scm else 'py'}",
                    link=str(randrange(10 ** 9)),
                    contents=content,
                    staff_only=True,
                    _impersonate="examtool",
                    timeout=5,
                )

                ag = (
                    url
                    + "\n"
                    + (status or "No issues")
                    + "\n"
                    + "\n".join(
                        ">>> "
                        + "\n... ".join(test.stmt.split("\n"))
                        + "\n"
                        + (
                            test.result
                            if test.result is not None
                            else "DID NOT EXECUTE"
                        )
                        for test in tests
                    )
                )

                # print(ag)

                out[email][template_name] = ag
                # input("continue?")

        roster = get_roster(exam=EXAM)

        # grade_student("aagulnick@berkeley.edu")
        # grade_student("ekandell@berkeley.edu")
        # grade_student("joshlor@berkeley.edu")
        # return

        if num_threads > 1:
            with ThreadPool(num_threads) as p:
                list(
                    tqdm(
                        p.imap_unordered(grade_student, [email for email, _ in roster]),
                        total=len(roster),
                    )
                )

        else:
            for email, _ in tqdm(roster):
                grade_student(email)

    finally:
        with open(f"{EXAM}_doctests.json", "w+") as f:
            dump(out, f)
