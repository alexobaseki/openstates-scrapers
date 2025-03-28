import scrapelib

import datetime
import os
import re
from collections import defaultdict
from functools import wraps

from openstates.scrape import Scraper, Bill, VoteEvent
from openstates.utils import convert_pdf
import lxml.html
import urllib

# Workaround to prevent chunking error (thanks @showerst)
#
# @see https://stackoverflow.com/a/37818792/1858091
import http.client

_HTTP_VSN = http.client.HTTPConnection._http_vsn
_HTTP_VSN_STR = http.client.HTTPConnection._http_vsn_str


def downgrade_http_version():
    http.client.HTTPConnection._http_vsn = 10
    http.client.HTTPConnection._http_vsn_str = "HTTP/1.0"


def undo_downgrade_http_version():
    http.client.HTTPConnection._http_vsn = _HTTP_VSN
    http.client.HTTPConnection._http_vsn_str = _HTTP_VSN_STR


def toggle_http_version(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        downgrade_http_version()
        response = method(self, *args, **kwargs)
        undo_downgrade_http_version()
        return response

    return wrapper


def action_type(action):
    """
    Used to standardise the bill actions to the terms specified
    :param scraped action:
    :return action classifier:
    """
    # http://www.scstatehouse.gov/actionsearch.php is very useful for this
    classifiers = (
        ("Adopted", "passage"),
        ("Amended and adopted", ["passage", "amendment-passage"]),
        ("Amended", "amendment-passage"),
        ("Certain items vetoed", "executive-veto-line-item"),
        ("Committed to", "referral-committee"),
        ("Committee Amendment Adopted", "amendment-passage"),
        (
            "Committee Amendment Amended and Adopted",
            ["amendment-passage", "amendment-amendment"],
        ),
        ("Committee Amendment Amended", "amendment-amendment"),
        ("Committee Amendment Tabled", "amendment-deferral"),
        ("Committee report: Favorable", "committee-passage-favorable"),
        ("Committee report: Majority favorable", "committee-passage"),
        ("House amendment amended", "amendment-amendment"),
        ("Introduced and adopted", ["introduction", "passage"]),
        ("Introduced, adopted", ["introduction", "passage"]),
        ("Introduced and read first time", ["introduction", "reading-1"]),
        ("Introduced, read first time", ["introduction", "reading-1"]),
        ("Introduced", "introduction"),
        ("Prefiled", "filing"),
        ("Read second time", "reading-2"),
        ("Read third time", ["passage", "reading-3"]),
        ("Recommitted to Committee", "referral-committee"),
        ("Referred to Committee", "referral-committee"),
        ("Rejected", "failure"),
        ("Senate amendment amended", "amendment-amendment"),
        ("Signed by governor", "executive-signature"),
        ("Signed by Governor", "executive-signature"),
        ("Tabled", "failure"),
        ("Veto overridden", "veto-override-passage"),
        ("Veto sustained", "veto-override-failure"),
        ("Vetoed by Governor", "executive-veto"),
    )
    for prefix, atype in classifiers:
        if action.lower().startswith(prefix.lower()):
            return atype
    # otherwise
    return None


def get_index_url(session, chamber, chamber_letter):
    """
    Note: a Web Archive ID should be added during session transitions to ensure
    functionality of past session scrapes.
    1. Search chamber-specific `index_url` on https://web.archive.org/
    2. Select latest snapshot in the session end year that still has bill intros
    listed for the full session (typically this is the early November snapshot)
    3. The Web Archive ID is the 14-digit number string in that snapshot's URL

    Example of 2019-2020 House bill index URL:
    https://web.archive.org/web/20201101155143/https://www.scstatehouse.gov/sessphp/hintros.php
    """
    index_url = f"https://www.scstatehouse.gov/sessphp/{chamber_letter}intros.php"

    # Dictionary of ids for URL paths to archived HTML of bill index pages
    # from the prior two sessions. (Archives needed or index_url leads to current session bills.)
    web_archive_ids = {
        "2017-2018-lower": "20181101144929",
        "2017-2018-upper": "20181101144422",
        "2019-2020-lower": "20201101155143",
        "2019-2020-upper": "20201101152857",
        "2021-2022-lower": "20221110101038",
        "2021-2022-upper": "20221110101352",
        # TODO: Add archive id values for both chambers for outgoing session
        #  during transition between sessions (Upcoming: Nov or Dec 2024).
    }

    web_archive_id = web_archive_ids.get(f"{session}-{chamber}", None)

    # Web Archive IDs should only be in collection for past sessions
    if web_archive_id:
        # Constructs web-archived page with given ids for past sessions
        web_arch_prepend = f"https://web.archive.org/web/{web_archive_id}"
        index_url = f"{web_arch_prepend}/{index_url}"

    return index_url


class SCBillScraper(Scraper):
    """
    Bill scraper that pulls down all legislation on from SC website.
    Used to pull in information regarding Legislation, and basic associated metadata,
    using x-path to find and obtain the information
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raise_errors = False
        self.retry_attempts = 5

    _subjects = defaultdict(set)

    @toggle_http_version
    def downgraded_http_get(self, url, params=None, **kwargs):
        return self.get(url, params=params, **kwargs)

    @toggle_http_version
    def downgraded_http_post(self, url, data=None, json=None, **kwargs):
        return self.post(url, data=data, json=json, **kwargs)

    def scrape_subjects(self, session):
        """
        Obtain bill subjects, which will be saved onto _subjects global,
        to be added on to bill later on in process.
        :param session:

        """
        # only need to do it once
        if self._subjects:
            return

        session_code = {
            "2013-2014": "120",
            "2015-2016": "121",
            "2017-2018": "122",
            "2019-2020": "123",
        }[session]

        subject_search_url = "https://www.scstatehouse.gov/subjectsearch.php"
        data = self.post(
            subject_search_url,
            data=dict(
                (
                    ("GETINDEX", "Y"),
                    ("SESSION", session_code),
                    ("INDEXCODE", "0"),
                    ("INDEXTEXT", ""),
                    ("AORB", "B"),
                    ("PAGETYPE", "0"),
                )
            ),
        ).text
        doc = lxml.html.fromstring(data)
        # skip first two subjects, filler options
        for option in doc.xpath("//option")[2:]:
            subject = option.text
            code = option.get("value")
            url = "%s?AORB=B&session=%s&indexcode=%s" % (
                subject_search_url,
                session_code,
                code,
            )

            # SC's server is sending some noncomplient server responses
            # that are confusing self.get
            # workaround via
            # https://stackoverflow.com/questions/14442222/how-to-handle-incompleteread-in-python
            try:
                self.info(url)
                data = urllib.request.urlopen(url).read()
            except http.client.IncompleteRead as e:
                self.warning("Client IncompleteRead error on {}".format(url))
                data = e.partial

            doc = lxml.html.fromstring(data)
            for bill in doc.xpath('//span[@style="font-weight:bold;"]'):
                match = re.match(r"(?:H|S) \d{4}", bill.text)
                if match:
                    # remove * and leading zeroes
                    bill_id = match.group().replace("*", " ")
                    bill_id = re.sub(" 0*", " ", bill_id)
                    self._subjects[bill_id].add(subject)

    def scrape_vote_history(self, bill, vurl):
        """
         Obtain the information on a vote and link it to the related Bill
        :param bill: related bill
        :param vurl: source for the voteEvent information.
        :return: voteEvent object
        """
        html = self.get(vurl).text
        doc = lxml.html.fromstring(html)
        doc.make_links_absolute(vurl)

        # skip first two rows
        for row in doc.xpath("//table/tr")[2:]:
            tds = row.getchildren()
            if len(tds) != 11:
                self.warning("irregular vote row: %s" % vurl)
                continue
            (
                timestamp,
                motion,
                vote,
                yeas,
                nays,
                nv,
                exc,
                pres,
                abst,
                total,
                result,
            ) = tds

            timestamp = timestamp.text.replace("\xa0", " ")
            timestamp = datetime.datetime.strptime(timestamp, "%m/%d/%Y %H:%M %p")

            yeas = int(yeas.text)
            nays = int(nays.text)
            others = int(nv.text) + int(exc.text) + int(abst.text) + int(pres.text)
            assert yeas + nays + others == int(total.text)

            if result.text == "Passed":
                passed = "pass"
            else:
                passed = "fail"

            vote_link = vote.xpath("a")[0]
            if "[H]" in vote_link.text:
                chamber = "lower"
            else:
                chamber = "upper"

            vote = VoteEvent(
                chamber=chamber,  # 'upper' or 'lower'
                start_date=timestamp.strftime("%Y-%m-%d"),  # 'YYYY-MM-DD' format
                motion_text=motion.text,
                result=passed,
                classification="passage",  # Can also be 'other'
                # Provide a Bill instance to link with the VoteEvent...
                bill=bill,
            )

            vote.set_count("yes", yeas)
            vote.set_count("no", nays)
            vote.set_count("other", others)

            vote.add_source(vurl)

            # obtain vote rollcall from pdf and add it to the VoteEvent object
            rollcall_pdf = vote_link.get("href")
            self.scrape_rollcall(vote, rollcall_pdf)
            vote.add_source(rollcall_pdf)
            if rollcall_pdf in self._seen_vote_ids:
                self.warning("duplicate usage of %s, skipping", rollcall_pdf)
                continue
            else:
                self._seen_vote_ids.add(rollcall_pdf)
            vote.dedupe_key = rollcall_pdf  # distinct KEY for each one

            yield vote

    def scrape_rollcall(self, vote, vurl):
        """
         Get text information from the pdf, containing the vote roll call
         and add the information obtained to the related voteEvent object
        :param vote:  related voteEvent object
        :param vurl:  pdf source url
        """
        (path, resp) = self.urlretrieve(vurl)
        pdflines = convert_pdf(path, "text")
        os.remove(path)

        current_vfunc = None
        option = None

        for line in pdflines.split(b"\n"):
            line = line.strip().decode()

            # change what is being recorded
            if line.startswith("YEAS") or line.startswith("AYES"):
                current_vfunc = vote.yes
            elif line.startswith("NAYS"):
                current_vfunc = vote.no
            elif line.startswith("EXCUSED"):
                current_vfunc = vote.vote
                option = "excused"
            elif line.startswith("NOT VOTING"):
                current_vfunc = vote.vote
                option = "excused"
            elif line.startswith("ABSTAIN"):
                current_vfunc = vote.vote
                option = "excused"
            elif line.startswith("PAIRED"):
                current_vfunc = vote.vote
                option = "paired"

            # skip these
            elif not line or line.startswith("Page "):
                continue

            # if a vfunc is active
            elif current_vfunc:
                # split names apart by 3 or more spaces
                names = re.split(r"\s{3,}", line)
                for name in names:
                    if name:
                        if not option:
                            current_vfunc(name.strip())
                        else:
                            current_vfunc(option=option, voter=name.strip())

    def scrape_details(self, bill_detail_url, session, chamber, bill_id):
        """
        Create the Bill and add the information obtained from the provided bill_detail_url.
        and then yield the bill object.
        :param bill_detail_url:
        :param session:
        :param chamber:
        :param bill_id:
        :return:
        """
        page = self.get(bill_detail_url).text

        if "INVALID BILL NUMBER" in page:
            self.warning("INVALID BILL %s" % bill_detail_url)
            return

        doc = lxml.html.fromstring(page)
        doc.make_links_absolute(bill_detail_url)

        bill_div = doc.xpath('//div[@style="margin:0 0 40px 0;"]')[0]

        bill_type = bill_div.xpath("span/text()")[0]

        if "General Bill" in bill_type:
            bill_type = "bill"
        elif "Concurrent Resolution" in bill_type:
            bill_type = "concurrent resolution"
        elif "Joint Resolution" in bill_type:
            bill_type = "joint resolution"
        elif "Resolution" in bill_type:
            bill_type = "resolution"
        else:
            raise ValueError("unknown bill type: %s" % bill_type)

        # Short "Summary" will be added as the bill title,
        #  while the longer summary will be added as an abstract
        summ_and_abst = [x.strip() for x in bill_div.xpath("./text()")]
        summary, abstract = [x for x in summ_and_abst if len(x)][-2:]

        bill = Bill(
            bill_id,
            legislative_session=session,  # session name metadata's `legislative_sessions`
            chamber=chamber,  # 'upper' or 'lower'
            title=summary,
            classification=bill_type,
        )

        # This stores the more lengthy 'summary' description as an abstract
        bill.add_abstract(note="description", abstract=abstract)

        subjects = list(self._subjects[bill_id])

        def _get_sponsor_chamber(url):
            url = url.get("href")
            return (
                "upper"
                if "chamber=S" in url
                else ("lower" if "chamber=H" in url else None)
            )

        for subject in subjects:
            bill.add_subject(subject)

        # sponsors
        for sponsor in doc.xpath('//a[contains(@href, "member.php")]'):
            sp_chamber = _get_sponsor_chamber(sponsor)
            sponsor = sponsor.text.strip()
            bill.add_sponsorship(
                name=sponsor,
                classification="primary",
                primary=True,
                entity_type="person",
                chamber=sp_chamber,
            )
        for sponsor in doc.xpath('//a[contains(@href, "committee.php")]'):
            sp_chamber = _get_sponsor_chamber(sponsor)
            sponsor = sponsor.text_content().replace("\xa0", " ").strip()
            bill.add_sponsorship(
                name=sponsor,
                classification="primary",
                primary=True,
                entity_type="organization",
                chamber=sp_chamber,
            )

        # find versions
        version_url = doc.xpath('//a[text()="View full text"]/@href')[0]
        version_html = self.get(version_url).text
        version_doc = lxml.html.fromstring(version_html)
        version_doc.make_links_absolute(version_url)
        for version in version_doc.xpath('//a[contains(@href, "/prever/")]'):
            bill.add_version_link(
                note=version.text,  # Description of the version from the state;
                #  eg, 'As introduced', 'Amended', etc.
                url=version.get("href"),
                on_duplicate="ignore",
                media_type="text/html",  # Still a MIME type
            )
        # for prefiles, the link just points right to the version, not to a versions page
        if "/bills/" in version_url.lower():
            bill.add_version_link(
                note="Filed",
                url=version_url,
                on_duplicate="ignore",
                media_type="text/html",  # Still a MIME type
            )
            bill.add_version_link(
                note="Filed",
                url=version_url.replace(".htm", ".docx"),
                on_duplicate="ignore",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        # actions
        for row in bill_div.xpath("table/tr"):
            date_td, chamber_td, action_td = row.xpath("td")

            date = datetime.datetime.strptime(date_td.text, "%m/%d/%y")
            action_chamber = {"Senate": "upper", "House": "lower", None: "legislature"}[
                chamber_td.text
            ]

            action = action_td.text_content()
            action = action.split("(House Journal")[0]
            action = action.split("(Senate Journal")[0].strip()

            atype = action_type(action)

            bill.add_action(
                description=action,  # Action description, from the state
                date=date.strftime("%Y-%m-%d"),  # `YYYY-MM-DD` format
                chamber=action_chamber,  # 'upper' or 'lower'
                classification=atype,  # Options explained in the next section
            )

        # votes
        vurl = doc.xpath('//a[text()="View Vote History"]/@href')
        if vurl:
            vurl = vurl[0]
            yield from self.scrape_vote_history(bill, vurl)

        bill.add_source(bill_detail_url)
        yield bill

    def scrape(self, chamber=None, session=None):
        """
         Obtain the bill urls containing the bill information which will be used
         by the scrape_details function to yield the desired Bill objects
        :param chamber:
        :param session:
        """
        self._seen_vote_ids = set()

        # Subject scraping disabled Summer 2020, openstates/issues#77
        # Leaving the remnants of this around since it is very possible that SC will
        # update their web configuration, and we can reuse this later, but for now it was
        # breaking 75% of the time, and it isn't worth the cost.
        # self.scrape_subjects(session)

        # get bill index
        chambers = [chamber] if chamber else ["upper", "lower"]

        # Regex used to extract non-archived (actual) bill list page links
        web_archive_re = re.compile(r"https://web\.archive\.org/web/\d+/(.+)")

        for chamber in chambers:
            chamber_letter = "s" if chamber == "upper" else "h"

            # Uses helper function to get current session bill index page url,
            # or archived url in case of past session
            index_url = get_index_url(session, chamber, chamber_letter)
            # TODO: Update Web Archive ID values in `get_index_url()` during session transitions

            page = self.get(index_url).text
            doc = lxml.html.fromstring(page)
            doc.make_links_absolute(index_url)

            # visit each day and extract bill ids
            days = doc.xpath("//div/b/a/@href")
            for day_url in days:

                # If bill intro links are retrieved from archived page
                web_archive_match = web_archive_re.match(day_url)
                if web_archive_match:
                    # Extract actual page using the regex match object
                    day_url = web_archive_match.group(1)

                try:
                    data = self.get(day_url).text
                except scrapelib.HTTPError:
                    continue

                doc = lxml.html.fromstring(data)
                doc.make_links_absolute(day_url)

                for bill_a in doc.xpath("//p/a[1]"):
                    bill_id = bill_a.text.replace(".", "")
                    if bill_id.lower().startswith(chamber_letter):
                        yield from self.scrape_details(
                            bill_a.get("href"), session, chamber, bill_id
                        )

            prefile_url = (
                f"https://www.scstatehouse.gov/sessphp/prefil{session[2:4]}.php"
            )
            page = self.get(prefile_url).text
            doc = lxml.html.fromstring(page)
            doc.make_links_absolute(prefile_url)

            # visit each day and extract bill ids
            if chamber == "lower":
                days = doc.xpath('//dd[contains(text(),"House")]/a/@href')
            else:
                days = doc.xpath('//dd[contains(text(),"Senate")]/a/@href')

            for day_url in days:
                try:
                    data = self.get(day_url).text
                except scrapelib.HTTPError:
                    continue

                doc = lxml.html.fromstring(data)
                doc.make_links_absolute(day_url)

                for bill_a in doc.xpath("//p/a[1]"):
                    bill_id = bill_a.text.replace(".", "")
                    if bill_id.lower().startswith(chamber_letter):
                        yield from self.scrape_details(
                            bill_a.get("href"), session, chamber, bill_id
                        )
