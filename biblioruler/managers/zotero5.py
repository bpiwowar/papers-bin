# zotero SQL backend

from pathlib import Path
import shutil

import biblioruler.managers.base as managers
from sqlalchemy.orm import scoped_session, sessionmaker
import html.parser

from dateutil import parser as dtparser
from urllib.parse import unquote
from urllib.parse import urlparse

from .base import Resource, HighlightAnnotation, NoteAnnotation
from biblioruler.sqlite3utils import dict_factory
import argparse
import sqlite3
import os
import os.path as op
import logging
import platform
import datetime as dt
import subprocess

from sqlalchemy import create_engine
import biblioruler.managers.db.zotero5 as dbz

import configparser
import re


# --- Utilities

DEFAULTS = None


def defaults():
    """Get defaults"""
    global DEFAULTS
    if DEFAULTS is not None:
        return DEFAULTS

    DEFAULTS = {}

    system = platform.system()
    pathtransform = lambda x: x


    if system == "Darwin":
        mainpath = os.path.expanduser("~/Library/Application Support/Zotero")
    elif system == "Linux":
        if "microsoft" in platform.uname().release:
            from pathlib import PureWindowsPath
            def pathtransform(path):
                p = PureWindowsPath(path)
                r= Path("/mnt") / p.drive[:-1].lower() /  Path(*p.parts[1:])
                return r
            path=subprocess.check_output('cmd.exe /c "echo %USERPROFILE%"', shell=True, stderr=subprocess.DEVNULL).strip().decode("utf-8")
            mainpath= pathtransform(path) / "AppData/Roaming/Zotero/Zotero"
        else:
            mainpath = os.path.expanduser("~/.zotero/zotero")
    else:
        raise Exception("No zotero path defined for %s" % platform.system())

    inipath = os.path.join(mainpath, "profiles.ini")
    logging.info("Reading %s", inipath)
    config = configparser.ConfigParser()
    config.read(inipath)

    profilepath = None
    for k, v in config.items():
        if k.startswith("Profile"):
            if v.get("Default", 0) == "1":
                profilepath = mainpath / v["Path"]
                break

    assert profilepath is not None, f"Could not find the default profile in {mainpath}/profiles.ini"

    # Read preferences
    prefs = os.path.join(profilepath, "prefs.js")
    re_pref = re.compile(r"""user_pref\("([^"]+)", "([^"]+)"\)""")
    with open(prefs, "rt", errors="ignore") as pfh:
        for line in pfh:
            m = re_pref.match(line)
            if m is not None:
                if m.group(1) == "extensions.zotero.baseAttachmentPath":
                    DEFAULTS["baseAttachmentPath"] = m.group(2)
                elif m.group(1) == "extensions.zotero.dataDir":
                    DEFAULTS["dataDir"] = str(pathtransform(m.group(2)))

    if "baseAttachmentPath" not in DEFAULTS:
        DEFAULTS["baseAttachmentPath"] = os.path.join(DEFAULTS["dataDir"], "storage")
    DEFAULTS["dbpath"] = os.path.join(DEFAULTS["dataDir"], "zotero.sqlite")
    logging.info("Zotero default: %s", DEFAULTS)

    return DEFAULTS


# --- Resources


@Resource(urn="zotero:note")
class Note(managers.Note):
    """A note"""

    def __init__(self, note: dbz.ItemNote):
        super().__init__(note.itemID, title=note.title, html=note.note)


@Resource(urn="zotero:collection")
class Collection(managers.Collection):
    """A collection"""

    def __init__(self, manager, collection: dbz.Collection):
        super().__init__(
            collection.collectionID, collection.collectionName, surrogate=True
        )
        self.manager = manager

    def _retrieve(self):
        query = (
            self.manager.session.query(dbz.CollectionItem)
            .filter(dbz.CollectionItem.collectionID == self.local_uuid)
            .join(dbz.Item)
            # .order_by(dbz.CollectionItem.orderIndex)
        )

        self.publications = []
        for collectionItem in query.all():
            self.publications.append(Paper(self.manager, collectionItem.item.key))

        self.surrogate = False


@Resource(urn="zotero")
class Author(managers.Author):
    """An author"""

    def __init__(self, author: dbz.ItemCreator):
        super().__init__(
            author.creatorID,
            firstname=author.creator.firstName,
            surname=author.creator.lastName,
            surrogate=False,
        )


@Resource(urn="zotero:paper")
class Paper(managers.Paper):
    """A zotero paper"""

    def __init__(self, manager, uuid):
        managers.Paper.__init__(self, uuid)
        self.manager = manager

    def _retrieve(self):
        try:
            self.populate(
                self.manager.session.query(dbz.Item)
                .filter(dbz.Item.key == self.local_uuid)
                .one()
            )
        except:
            logging.exception("Could not retrieve item %s", self.local_uuid)
            raise

    def populate(self, item: dbz.Item):
        self.init()
        values = {data.field.fieldName: data.value.value for data in item.data}
        self.title = values.get("title", None)
        self.uri = "zotero://select/items/1_%s" % self.local_uuid

        self.notes = [Note(note) for note in item.notes]
        self.authors = [Author(author) for author in item.creators]
        self.tags = set(tag.tag.name for tag in item.tags)

        # Retrieve the date
        date = values.get("date", None)
        if date:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date)
            if m:
                self.year = int(m.group(1))
                self.month = int(m.group(2))
                self.day = int(m.group(3))
        self.creationdate = item.dateAdded

        self.surrogate = False


class Manager(managers.Manager):
    def connect(self):
        # Read only connect
        return sqlite3.connect("file:%s?mode=ro" % self.ro_dbpath, uri=True)

    """zotero manager"""

    def __init__(
        self,
        dbpath=defaults()["dbpath"],
        filebase=defaults()["baseAttachmentPath"],
        copy=True,
    ):
        """Initialize the manager"""
        managers.Manager.__init__(self, None, surrogate=False)
        self.dbpath = Path(dbpath)
        self.ro_dbpath = self.dbpath
        self.filebase = filebase

        if copy:
            self.ro_dbpath = self.dbpath.with_suffix(".ro.sql")

        self.engine = None
        self.refresh()

    def refresh(self):
        """Copy again the database if read-only and outdated"""
        print("Copy to", self.ro_dbpath)

        if self.ro_dbpath:
            if (
                not self.ro_dbpath.is_file()
                or self.ro_dbpath.stat().st_mtime < self.dbpath.stat().st_mtime
            ):
                shutil.copyfile(self.dbpath, self.ro_dbpath)
                self.engine = None

        if not self.engine:
            self.engine = create_engine(
                u"sqlite://", creator=self.connect, connect_args={"readonly": True}
            )
            # options={ "mode": "ro"})
            self.session = scoped_session(sessionmaker(bind=self.engine))

            logging.info("Connected to Zotero SQL database")

            self.fields = {}
            for row in self.session.query(dbz.FieldsCombined):
                self.fields[row.fieldName] = row.fieldID

    def collections(self):
        return None

    def get_collection_by_key(self, key):
        collection = (
            self.session.query(dbz.Collection).filter(dbz.Collection.key == key).one()
        )
        return Collection(self, collection)

    def get_item_by_key(self, key):
        return Paper(self, key)

    def get_paper_by_key(self, key):
        return Paper(self, key)

    def get_publication_by_uri(self, uri):
        re_papers_uri = re.compile(r"^zotero://select/items/\d+_(.*)$")
        m = re_papers_uri.match(uri)
        if m is None:
            logging.warn("%s is not a Zotero URI", uri)
            return None
        else:
            uriref = m.group(1)
            logging.debug("Searching for Zotero publication with UUID %s", uriref)
            item = (
                self.session.query(dbz.Item)
                .outerjoin(dbz.DeletedItem)
                .filter(dbz.DeletedItem.itemID == None)
                .filter(dbz.Item.key == uriref)
                .one()
            )

            return Paper(self, item.key)

    def find_by(self, key, value):
        query = (
            self.session.query(dbz.ItemData, dbz.Item)
            .join(dbz.FieldsCombined)
            .join(dbz.ItemDataValue)
            .join(dbz.Item)
            .outerjoin(dbz.DeletedItem)
            .filter(dbz.DeletedItem.itemID == None)
            .filter(dbz.ItemData.fieldID == self.fields[key])
            .filter(dbz.ItemDataValue.value.like(value))
        )

        logging.debug("Retrieving Zotero paper by %s == [%s]: %s", key, value, query)
        papers = []
        for data, item in query:
            papers.append(Paper(self, item.key))

        return papers

    def find_by_doi(self, doi):
        return self.find_by("DOI", doi)

    def find_by_title(self, title):
        return self.find_by("title", title)

    @staticmethod
    def create(prefix, args):
        """Creates a new manager"""
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument(
            "--%shelp" % prefix,
            action="help",
            help="Provides helps about arguments for this manager",
        )
        args, remaining_args = parser.parse_known_args(args)
        return Manager(args.dbpath), remaining_args
