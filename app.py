import sys
import os
import urllib.parse

from rdflib.plugins.parsers.notation3 import BadSyntax
from services.fuseki_service import FusekiService as fs

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(CURRENT_DIR))

from functools import wraps
from typing import Callable
from datetime import timedelta
from utils.utils import lookup_model_uri_from_modelverzoek_uri
from utils.utils import MeldingSoort as ms
from utils.utils import AttribuutMelding as meldattr
from utils.utils import MeldingManager
from urllib.parse import unquote

import inspect
import logging
import os.path
import traceback


import warnings
import re
import tempfile
import pandas as pd
from flask import (
    Flask,
    request,
    render_template,
    Response,
    send_file,
    send_from_directory,
    jsonify,
    redirect,
    abort,
)
import json

from runner.turtle_creator import TurtleCreator

from utils import constants as cn
import psycopg2
import uuid
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD
import frontend.ep.config as config
import frontend.application_fields as afn
import os
import io
import subprocess
from datetime import datetime, timedelta
from requests_oauthlib import OAuth2Session
from icecream import ic

# from flask_cors import CORS
import requests
import pyshacl

dir_path = None
model_name = None
turtle_url = None
original_route = None
usergroep = None
username = None
routes = json.load(
    open(
        os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        + os.sep
        + "frontend"
        + os.sep
        + "ep"
        + os.sep
        + "mb-json-routes.json"
    )
)
app = Flask(__name__)


def add_melding_manager(func: Callable):
    """Decorator that passes a MeldingManager object to the function and adds it to the JSON response."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        melding_manager = MeldingManager()  # Initialize a new context
        response = func(
            *args, melding_manager=melding_manager, **kwargs
        )  # Call the wrapped function

        # Handle tuple responses (Response, status_code)
        if isinstance(response, tuple):
            if not isinstance(
                response[0], Response
            ):  # Ensure first element is a Flask Response
                raise TypeError(
                    f"Expected function '{func.__name__}' to return a Flask Response, but got {type(response[0]).__name__}"
                )
            response, status = response  # Unpack the tuple
        else:
            if not isinstance(
                response, Response
            ):  # Ensure response is a Flask Response
                raise TypeError(
                    f"Expected function '{func.__name__}' to return a Flask Response, but got {type(response).__name__}"
                )
            status = None  # No explicit status code provided

        # Extract JSON data from response
        data = response.get_json()
        if not isinstance(data, dict):  # Ensure the response contains a dictionary
            raise TypeError(
                f"Expected JSON response to be a dictionary, but got {type(data).__name__}"
            )

        # Add the MeldingManager data
        data["meldingen"] = (
            melding_manager.meldingen
        )  # Store only the list of meldingen

        # Repackage the response
        return (jsonify(data), status) if status else jsonify(data)

    return wrapper


def one_time_add_table_toetsingsrapport_to_database():
    query = cn.POSTGRES_MAPPING["toetsingsrapport"][cn.CREATE_QUERY]
    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        db_connection.commit()
    except Exception as e:
        app.logger.exception(
            f"ERROR: An exception occurred:\n{traceback.format_stack()}"
        )


one_time_add_table_toetsingsrapport_to_database()  # TODO: REMOVE ALTER TABLE ADD COLUMN after toetsingsrapport-upload works stable in production


def one_time_add_table_kennisgebiedenregister_bestand_to_database():
    query = cn.POSTGRES_MAPPING["kennisgebiedenregister_bestand"][cn.CREATE_QUERY]
    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        db_connection.commit()
    except Exception as e:
        app.logger.exception(
            f"ERROR: An exception occurred:\n{traceback.format_stack()}"
        )


one_time_add_table_kennisgebiedenregister_bestand_to_database()  # TODO: REMOVE ALTER TABLE ADD COLUMN after toetsingsrapport-upload works stable in production


def one_time_add_table_rapportage_query_to_database():
    query = cn.POSTGRES_MAPPING["rapportage_query"][cn.CREATE_QUERY]
    app.logger.info("performing query: " + query)

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        db_connection.commit()
    except Exception as e:
        app.logger.exception(
            f"ERROR: An exception occurred:\n{traceback.format_stack()}"
        )


one_time_add_table_rapportage_query_to_database()


def visits_unaccessible_page(role, path) -> bool:
    path = path.lstrip("/")
    # Get the set of protected pages the current user's role has explicit access to
    EXPLICIT_ACCESS_PAGES = set(config.PROTECTED_PAGES_MAPPING.get(role, []))
    app.logger.info(f"EXPLICIT_ACCESS_PAGES is {EXPLICIT_ACCESS_PAGES}")

    app.logger.info(f"PROTECTED_PAGES_SET is {config.PROTECTED_PAGES_SET}")

    # Determine which pages the user does NOT have access to
    UNACCESSIBLE_PAGES = config.PROTECTED_PAGES_SET - EXPLICIT_ACCESS_PAGES
    app.logger.info(f"UNACCESSIBLE_PAGES is {UNACCESSIBLE_PAGES}")

    return path in UNACCESSIBLE_PAGES  # returns True or False


def visits_unaccessible_endpoint(role, endpoint) -> bool:
    # Get the set of protected pages the current user's role has explicit access to
    EXPLICIT_ACCESS_ENDPOINTS = set(config.PROTECTED_ENDPOINTS_MAPPING.get(role, []))
    app.logger.info(f"EXPLICIT_ACCESS_ENDPOINTS is {EXPLICIT_ACCESS_ENDPOINTS}")

    app.logger.info(f"PROTECTED_ENDPOINTS_SET is {config.PROTECTED_ENDPOINTS_SET}")

    # Determine which pages the user does NOT have access to
    UNACCESSIBLE_ENDPOINTS = config.PROTECTED_ENDPOINTS_SET - EXPLICIT_ACCESS_ENDPOINTS
    app.logger.info(f"UNACCESSIBLE_ENDPOINTS is {UNACCESSIBLE_ENDPOINTS}")

    return endpoint in UNACCESSIBLE_ENDPOINTS  # returns True or False


# Global before request hook for checking role access
@app.before_request
def before(func_request=None):
    """
    Function requires 'usergroep' to have been defined in the app before being called it

    """
    request_to_handle = request if func_request is None else func_request

    url = request_to_handle.url

    endpoint = request_to_handle.endpoint

    page = (
        request_to_handle.full_path.lstrip("/").lstrip("?").lstrip("page=").rstrip("?")
    )

    # ic(request_to_handle.full_path)
    # ic(request_to_handle.access_route)
    # ic(request_to_handle.base_url)
    # ic(request_to_handle.endpoint)
    # ic(request_to_handle.root_url)
    # ic(request_to_handle.url)

    # # Endpoint 3: User information
    # userinfo_headers = {"Authorization": f'Bearer {token_data["access_token"]}'}
    # userdata = requests.get(
    #     config.USERINFO_URL, headers=userinfo_headers, verify=False
    # ).json()
    # username = userdata[afn.USER_NAME]
    # ic(username)
    app.logger.info(f"request_to_handle.args = {request_to_handle.args}")

    app.logger.info(
        f"{username} is requesting access to: {page} as role: [{usergroep}] - (full url is: {url})"
    )
    if visits_unaccessible_page(usergroep, page):
        app.logger.info(
            f"ACCESS DENIED to {usergroep} for page: {page} with endpoint {endpoint}"
        )
        abort(403)  # Deny access with a 403 Forbidden response

    if visits_unaccessible_endpoint(usergroep, endpoint):
        app.logger.info(
            f"ACCESS DENIED to {usergroep} for endpoint: {endpoint} with page {page}"
        )
        abort(403)  # Deny access with a 403 Forbidden response

    app.logger.info(
        f"ACCESS GRANTED to {usergroep} for: {page} - (full url is: {url}) with endpoint: {endpoint}"
    )

    # Proceed with the original function if access is granted
    pass


@app.errorhandler(403)
def forbid_access(error):
    return (
        jsonify(
            {
                "result": "Geen toegang tot deze functionaliteit",
                "original route": original_route,
                "usergroep": usergroep,
            }
        ),
        403,
    )


if config.ENV in ["ONT", "TST", "ACC", "PROD"]:

    @app.route("/", methods=["GET", "POST"])
    @app.route("/<route>", methods=["GET", "POST"])
    @app.route(config.SSO_ROUTE, methods=["GET", "POST"])
    def index_with_sso(route=None):
        global original_route
        global usergroep
        global username
        original_route = (
            request.full_path.replace("/", "")
            if "/authorized" not in request.path
            else original_route
        )
        original_route = "" if original_route == "?" else original_route

        if afn.CODE in request.args:
            # User is logged in
            # Get Authorization Code
            # Necessary for authorizing the webserver to get access to the users' access_token within a specific scope from the Federal Identity Bridge
            authorization_code = request.args.get(afn.CODE)

            # Endpoint 2: Get Access Token
            # With the Users' Access Token we can then access his/her info in the Federal Identity Bridge
            token_params = {
                afn.GRANT_TYPE: afn.AUTHORIZATION_CODE,
                afn.CODE: authorization_code,
                afn.CLIENT_ID: config.CLIENT_ID,
                afn.CLIENT_SECRET: config.CLIENT_KEY,
                afn.REDIRECT_URI: config.REDIRECT_URL,
            }
            try:
                token_data = requests.post(
                    config.TOKEN_URL,
                    data=token_params,
                    headers=afn.TOKEN_HEADER,
                    verify=False,
                ).json()
                if "access_token" in token_data:
                    access_token = token_data["access_token"]
                else:
                    app.logger.exception(
                        f"ERROR: An exception occurred, access_token missing:\n{traceback.format_stack()}"
                    )

            except KeyError as k:
                app.logger.exception(
                    f"ERROR: An exception occurred, access_token missing:\n{traceback.format_stack()}"
                )
            except Exception as e:
                app.logger.exception(
                    f"ERROR: An exception occurred, access_token missing:\n{traceback.format_stack()}"
                )

            # Endpoint 3: Get User information (name, group, roles)
            userinfo_headers = {"Authorization": f"Bearer {access_token}"}
            userdata = requests.get(
                config.USERINFO_URL, headers=userinfo_headers, verify=False
            ).json()

            # oude Mapping behouden voor commit op 20-9-2024, als je dit vind in productie na 1-11-2024 mag het weg
            # Mapping for menu options based on usergroep
            # usermenu_mapping = json.load(open("frontend/ep/mb-json-groep.json"))
            # users_mapping = json.load(open("frontend/ep/mb-json-users.json"))

            # Mapping for menu options based on usergroep
            usermenu_mapping = json.load(
                open(
                    os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                    + os.sep
                    + "frontend"
                    + os.sep
                    + "ep"
                    + os.sep
                    + "mb-json-groep.json"
                )
            )
            users_mapping = json.load(
                open(
                    os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                    + os.sep
                    + "frontend"
                    + os.sep
                    + "ep"
                    + os.sep
                    + "mb-json-users.json"
                )
            )
            username = userdata[afn.USER_NAME]

            if username not in users_mapping.keys():
                usergroep = "dflt"
                app.logger.debug(f"username not in users_mapping.keys")

            else:
                usergroep = users_mapping[username]
                app.logger.debug(f"usergroep = {usergroep}")

            usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
            usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]
            # TODO: Verwijder bovenstaande regels wanneer gebruikersrollen/usergroep door de Identity Bridge worden gehaald uit IMS

            # Perform check to see if user is allowed to access this page
            before(request)

            sso_response = render_template(
                "index.html",
                usernaam=userdata[afn.NAME],
                userid=userdata[afn.USER_NAME],
                usergroepnaam=usergroepnaam,
                usermenu=usermenu,
                parameters=original_route,
            )
            return sso_response
        else:
            # Endpoint 1b - redirect naar oauth
            oauth = OAuth2Session(
                redirect_uri=config.REDIRECT_URL,
                client_id=config.CLIENT_ID,
                scope=config.SCOPE,
            )
            authorization_url, authorization_code = oauth.authorization_url(
                config.AUTHORIZATION_URL
            )
            return redirect(authorization_url)

elif config.ENV == "LOC":
    app.logger.setLevel(logging.INFO)
    username = "MBK_MA"
    # Mapping for menu options based on usergroep
    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    # NOTE: Om lokaal te kunnen testen, kan de gebruikersrol (beh/mau/das/mod) worden aangepast om verschillende functies te zien

    @app.route("/", methods=["GET"])
    @app.route("/<route>", methods=["GET", "POST"])
    @app.route(config.SSO_ROUTE, methods=["GET", "POST"])
    def index_without_sso(route=""):
        global original_route
        global usergroep
        original_route = request.full_path.replace("/", "")

        # oude Mapping behouden voor commit op 20-9-2024, als je dit vind in productie na 1-11-2024 mag het weg
        # Mapping for menu options based on usergroep
        # usermenu_mapping = json.load(open("frontend/ep/mb-json-groep.json"))
        # users_mapping = json.load(open("frontend/ep/mb-json-users.json"))

        # Mapping for menu options based on usergroep
        usermenu_mapping = json.load(
            open(
                os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                + os.sep
                + "frontend"
                + os.sep
                + "ep"
                + os.sep
                + "mb-json-groep.json"
            )
        )
        users_mapping = json.load(
            open(
                os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                + os.sep
                + "frontend"
                + os.sep
                + "ep"
                + os.sep
                + "mb-json-users.json"
            )
        )
        print("inside index function", flush=True)

        if username not in users_mapping.keys():
            usergroep = "dflt"
            app.logger.info(f"username {username} not in users_mapping.keys")

        else:
            usergroep = users_mapping[username]
            app.logger.info(f"usergroep = {usergroep}")

        usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
        usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]
        # TODO: Verwijder bovenstaande regels wanneer gebruikersrollen/usergroep door de Identity Bridge worden gehaald uit IMS

        if visits_unaccessible_page(usergroep, request.path):
            abort(403)
        else:
            pass

        return render_template(
            "index.html",
            usernaam=f"LOCAL | {username}",
            userid=username,
            usergroepnaam=usergroepnaam,
            usermenu=usermenu,
            parameters=original_route,
        )


@app.route("/env", methods=["GET"])
def get_env():
    return jsonify({"titel": "ENV", "resultaat": "OK", "APP_ENV": config.ENV}), 200


# [TO DO] moet naar gekeken worden of we dit kunnen blijven gebruiken
@app.route(config.QUERY_ROUTE, methods=["POST"])
def query_fuseki():
    """
    This function expects a query_id and uses that together with query_parameters to construct a workable query.
    This query will be executed in Fuseki and results will be returned to client in json format

    """
    query_table = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "static"
            + os.sep
            + "mb-json-query.json"
        )
    )

    query_id = request.json["query_id"]
    query_parameters = request.json["query_parameters"]
    query = "".join(query_table[query_id])

    if len(query_parameters) > 0:
        for param in query_parameters:
            query = query.replace(f"<@{param}@>", query_parameters[param])

    # response = requests.post(
    #     config.DATASTORE_LOOKUP_ENDPOINT, data={"query": query}, verify=False
    # )

    response = fs.get_json(query)

    return response


@app.route("/fuseki", methods=["POST", "GET"])
def fuseki_binnendoor():
    """
    This function expects a query.
    This query will be executed in Fuseki and results will be returned to client as if it was directly talking to fuseki

    """
    try:
        query = request.form["query"]
    except KeyError:
        query = request.args.get(
            "query", ""
        )  # Blijkbaar is bij het klikken op triples in de magazijn niet een form query maar een 'Query string parameter'

    app.logger.info(f"query = {query}")

    content_type = request.accept_mimetypes
    app.logger.info(f"content_type = {content_type}")
    if "json" in str(content_type):
        app.logger.info(f"content_type = json")
        return fs.get_json(query)

    elif "text" in str(content_type):
        app.logger.info(f"content_type = ttl text")
        return fs.get_ttl(query)
    else:
        return jsonify()


@app.route(config.UPLOAD_ROUTE, methods=["POST"])
@add_melding_manager
def upload_file(melding_manager):
    global render_result
    global dir_path
    global model_name
    global turtle_url

    file = request.files["fileToUpload"]
    file_name = file.filename.replace(" ", "_")
    file_name_splitted = file_name.split(".")
    file_extension = "." + file_name_splitted[-1:][0]
    model_name = file_name.replace(file_extension, "")

    if (not file_extension.startswith(".xl")) and (file_extension != cn.LDM_EXTENSION):
        # and (file_extension != sbm_cn.XML_EXTENSION):
        error_msg = "Verkeerd bestandstype geselecteerd. Kies een BMS of Powerdesigner LDM bestand."
        melding_manager.meld(ms.ERROR, error_msg, "fileToUpload")
        return (
            jsonify(
                {
                    "titel": "Model afgewezen",
                    "aanleiding": "Het model is afgewezen om de volgende redenen:",
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                }
            ),
            200,
        )
    else:
        # Pre-work for the transformation
        upload_data = dict(request.form)
        file_content_bytes = file.read()
        dir_path = tempfile.mkdtemp()
        input_file_path = os.path.join(dir_path, file_name)
        with open(input_file_path, "wb") as f:
            f.write(file_content_bytes)
        try:
            # Transformation in code
            model_type = upload_data["soortModel"]
            turtle_creator = TurtleCreator(
                [rf"{input_file_path}"], model_type=model_type
            )
            turtle_creator.create_turtle()
            melding_manager += turtle_creator.melding_manager
            print("VDA naam = ", turtle_creator.get_vda_model_name())
            print("turtle_creator.melding_manager = " + melding_manager.to_str())
            print(melding_manager.to_str())

        except ValueError as v:
            app.logger.exception(
                f"ERROR: An exception occurred:\n{traceback.format_stack()}"
            )
            error_msg = f"\nTransformatie mislukt.\n{v}"
            melding_manager.meld(ms.ERROR, error_msg, "fileToUpload")

            return (
                jsonify(
                    {
                        "titel": "Model afgewezen",
                        "aanleiding": "Het model is afgewezen om de volgende redenen:",
                        "resultaat": f"ERROR",
                        "opmerking": error_msg,
                    }
                ),
                200,
            )

        except Exception as e:
            app.logger.exception(
                f"ERROR: An exception occurred:\n{traceback.format_stack()}"
            )
            error_msg = f"\nTransformatie mislukt.\nDe inhoud van het bestand kon niet op de juiste wijze worden omgezet. Controleer of uw bestand de juiste opbouw heeft. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            melding_manager.meld(ms.ERROR, error_msg, "fileToUpload")

            return (
                jsonify(
                    {
                        "titel": "Model afgewezen",
                        "aanleiding": "Het model is afgewezen om de volgende redenen:",
                        "resultaat": f"ERROR",
                        "opmerking": error_msg,
                    }
                ),
                200,
            )

        vda_model_naam = turtle_creator.get_vda_model_name()
        turtle_url = config.DATASTORE_ENDPOINT + "?graph=" + turtle_creator.uri
        named_graph = turtle_url.replace(config.DATASTORE_ENDPOINT + "?graph=", "")
        turtle_content = requests.get(turtle_url, verify=False).text.encode("utf-8")
        app.logger.info(f"named_graph from transformation: {named_graph}")

        file_id = uuid.uuid4().hex

        # if transformation is successful, upload in the document store
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        with db_cursor:
            create_table_query = f"CREATE TABLE IF NOT EXISTS Modelverzoeken (file_id varchar(100), model_name varchar(100), vda_model_name varchar(100), binary_data bytea, file_extension varchar(5), named_graph varchar(1000), upload_moment timestamp, bd_user varchar(7), domain varchar(100), title_request varchar(100), request_type varchar(100), model_type varchar(100), model_status varchar(100), jira_number varchar(10), extra_modellers varchar(100), remarks varchar(5000))"
            insert_query = f"INSERT INTO Modelverzoeken (file_id, model_name, vda_model_name, binary_data, file_extension, named_graph, upload_moment, bd_user, domain, title_request, request_type,  model_type, model_status, jira_number, extra_modellers, remarks,modelverzoek_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            db_cursor.execute(create_table_query)
            # Table insert 1: original document
            db_cursor.execute(
                insert_query,
                (
                    f"{file_id}-{file_name_splitted[-1:][0]}",
                    model_name,
                    vda_model_naam,
                    file_content_bytes,
                    file_extension,
                    named_graph,
                    datetime.now(),
                    upload_data[afn.BDUSER],
                    upload_data[afn.DOMAIN],
                    upload_data[afn.TITEL_VERZOEK],
                    upload_data[afn.SOORT_VERZOEK],
                    upload_data[afn.SOORT_MODEL],
                    upload_data[afn.MODEL_STATUS],
                    upload_data[afn.SD_JIRA_ITEM_NUMMER],
                    upload_data[afn.EXTRA_MODELLEURS],
                    upload_data[afn.REMARKS],
                    cn.mb_json_options["modelVerzoekStatus"]["options"]["Ingediend"],
                ),
            )
            # Table insert 2: transformed model = turtle
            db_cursor.execute(
                insert_query,
                (
                    f"{file_id}-ttl",
                    model_name,
                    vda_model_naam,
                    turtle_content,
                    cn.TURTLE_EXTENSION,
                    named_graph,
                    datetime.now(),
                    upload_data[afn.BDUSER],
                    upload_data[afn.DOMAIN],
                    upload_data[afn.TITEL_VERZOEK],
                    upload_data[afn.SOORT_VERZOEK],
                    upload_data[afn.SOORT_MODEL],
                    upload_data[afn.MODEL_STATUS],
                    upload_data[afn.SD_JIRA_ITEM_NUMMER],
                    upload_data[afn.EXTRA_MODELLEURS],
                    upload_data[afn.REMARKS],
                    cn.mb_json_options["modelVerzoekStatus"]["options"]["Ingediend"],
                ),
            )
            db_connection.commit()
            db_cursor.close()
            db_connection.close()

            # Writing to Toestingslogboek Knowledge Graph
            model_verzoek_uri = URIRef(
                "http://modellenbibliotheek.belastingdienst.nl/id/verzoek/"
                + str(file_id)
            )
            current_toetsingslogboek = requests.get(
                config.TOETSINGSLOGBOEK_ENDPOINT, verify=False
            ).text.encode()
            g = Graph()
            g.parse(current_toetsingslogboek, format="turtle")
            g.bind("tlb", cn.NS_TLB)
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Verzoek))

            if upload_data[afn.SOORT_VERZOEK] == "TPV":
                g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Publicatieverzoek))
            elif upload_data[afn.SOORT_VERZOEK] == "FBV":
                g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Feedbackverzoek))

            g.add(
                (model_verzoek_uri, RDFS.label, Literal(upload_data[afn.TITEL_VERZOEK]))
            )

            g.add((model_verzoek_uri, cn.NS_TLB.betreft, URIRef(named_graph)))
            g.add(
                (
                    model_verzoek_uri,
                    cn.NS_TLB.datumVerzoek,
                    Literal(datetime.now().date(), datatype=XSD.date),
                )
            )
            g.add(
                (
                    model_verzoek_uri,
                    cn.NS_TLB.ingediendDoor,
                    URIRef(
                        "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                        + upload_data[afn.BDUSER]
                    ),
                )
            )
            g.add(
                (
                    model_verzoek_uri,
                    cn.NS_MB.code,
                    Literal(upload_data[afn.SD_JIRA_ITEM_NUMMER]),
                )
            )
            g.add(
                (
                    model_verzoek_uri,
                    cn.NS_TLB.jiraLink,
                    URIRef(
                        "https://jira.belastingdienst.nl/servicedesk/customer/portal/78/"
                        + upload_data[afn.SD_JIRA_ITEM_NUMMER]
                    ),
                )
            )
            if upload_data[afn.EXTRA_MODELLEURS] != "":
                betrokken_personen = re.split(
                    r"[,\s;]+", upload_data[afn.EXTRA_MODELLEURS]
                )
                for persoon in betrokken_personen:
                    g.add(
                        (
                            model_verzoek_uri,
                            cn.NS_TLB.betrokken,
                            URIRef(
                                "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                                + persoon
                            ),
                        )
                    )
            g.add((model_verzoek_uri, cn.NS_TLB.status, cn.NS_TLB.Ingediend))
            with open("a.txt", "wb+") as f:
                f.write(g.serialize(format="turtle").encode("utf-8"))

            requests.put(
                config.TOETSINGSLOGBOEK_ENDPOINT,
                data=g.serialize(format="turtle").encode("utf-8"),
                headers=cn.turtle_headers,
                verify=False,
            )
    opmerking = (
        f"\n🐢🐢🐢"
        f"\nmodel '{vda_model_naam}' uit het bestand '{file_name}' is succesvol ingediend."
        f"\nModelverzoeknaam: '{upload_data[afn.TITEL_VERZOEK]}'."
        f"\nModelverzoek uri: '{model_verzoek_uri}'."
    )

    melding_manager.meld(ms.SUCCESS, opmerking)

    return (
        jsonify(
            {
                "resultaat": f"OK",
            }
        ),
        200,
    )


@app.route(config.TOEVOEGEN_GEBRUIKERS, methods=["POST"])
def adding_users():
    file = pd.ExcelFile(request.files["fileToUpload"])
    dfs = {sheet_name: file.parse(sheet_name) for sheet_name in file.sheet_names}
    existing_users = json.load(
        # open(os.getcwd() + "\ep\mb-json-users.json".replace("\\", os.sep))
        os.getcwd() + os.sep + "ep" + os.sep + "mb-json-users.json",
        "w",
    )

    try:
        for sheet in file.sheet_names:
            usernames_to_add = dfs[sheet]["Username"]
            existing_users.update(
                {user_id: afn.MAPPING_USERGROUPS[sheet] for user_id in usernames_to_add}
            )
        with open(
            # os.getcwd() + "\ep\mb-json-users.json".replace("\\", os.sep), "w"
            os.getcwd() + os.sep + "ep" + os.sep + "mb-json-users.json",
            "w",
        ) as json_file:
            json.dump(existing_users, json_file, indent=4)
        return "Gebruikers toegevoegd!"
    except ():
        return "FOUT: Gebruikers toevoegen is mislukt."


@app.route(config.DOWNLOAD_ROUTE)
def retrieve_models_documentstore():
    db_connection = psycopg2.connect(**config.DB_CONFIG)
    db_cursor = db_connection.cursor()

    with db_cursor:
        db_cursor.execute(
            f"SELECT model_name, file_extension, named_graph, upload_moment, model_status, file_id FROM Modellen"
        )
        db_connection.commit()
        models = db_cursor.fetchall()
        db_cursor.close()
        db_connection.close()
    return jsonify(models)


@app.route("/original/<string:uri>")
def retrieve_original_model(uri: str):
    # TODO: turtle en original moeten niet twee rijen zijn in de sql database,
    #  in plaats daarvan moeten files een eigen tabel worden met als
    #  primary key de file_id en met overige kolommen de file_extension, en foreign key jira_number

    db_connection = psycopg2.connect(**config.DB_CONFIG)
    db_cursor = db_connection.cursor()
    query = f"SELECT binary_data, vda_model_name, file_extension, model_type FROM Modelverzoeken WHERE named_graph = %s AND file_extension != '.ttl'"
    values = [uri]
    with db_cursor:
        db_cursor.execute(query, values)
        db_connection.commit()
        result = db_cursor.fetchone()
        db_cursor.close()
        db_connection.close()

        if result is None:
            return "Bestand niet gevonden."

        file_content, vda_model_name, file_extension, type = result
    # CODE (waarschijnlijk van scott) HIERONDER
    model_urn = str("<" + uri + ">")
    app.logger.info(f"model_urn {model_urn}")
    query = f"""
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX mb: <http://modellenbibliotheek.belastingdienst.nl/def/mb#>
        PREFIX tlb: <http://modellenbibliotheek.belastingdienst.nl/def/tlb#>
        SELECT ?label ?type
        WHERE {{
        GRAPH {model_urn} {{
        {model_urn} rdfs:label ?label.
        {model_urn} mb:versieVan ?model.
        ?model a ?modeltype.
        FILTER (?modeltype!=mb:Model)
        }}
        OPTIONAL {{
        GRAPH {model_urn} {{
        {model_urn} mb:versieVan ?model.
        ?model a ?modeltype.
        FILTER (?modeltype!=mb:Model)
        }}
        GRAPH <urn:name:types> {{
        ?modeltype rdfs:label ?type.
        }}
        }}
        }} 
        """
    response2 = requests.post(
        config.DATASTORE_ENDPOINT, data={"query": query}, verify=False
    )
    if response2.status_code == 200:
        response_json = str(response2.json())
        # Convert JSON String to Python
        print("Response %s" % response2.status_code)
        results = json.loads(response_json.replace("'", '"'))

        for result in results["results"]["bindings"]:
            print(result["label"]["value"])  # Akten 1.0.0
            version_model = result["label"]["value"]
            print(result["type"]["value"])  # SBM
            type_model = result["type"]["value"]

            file_name = type_model + " " + version_model + file_extension
            # file_name = f"{type}{vda_model_name}{file_extension}"

    # response.headers["Content-Disposition"] = f'attachment;filename="{filename}.ttl"'

    return send_file(
        io.BytesIO(file_content),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=file_name,
    )


@app.route("/turtle/<string:uri>")
def retrieve_published_model(uri: str):
    # TODO: turtle en original moeten niet twee rijen zijn in de sql database,
    #  in plaats daarvan moeten files een eigen tabel worden met als
    #  primary key de file_id en met overige kolommen de file_extension, en foreign key jira_number

    turtle_url = config.DATASTORE_ENDPOINT + "?graph=" + uri
    turtle_content = requests.get(turtle_url, verify=False).content.decode("utf-8")
    response = Response(turtle_content, content_type="text/turtle")
    cut_content = turtle_content[turtle_content.find("mb:Modelversie") :]
    # CODE (waarschijnlijk van scott) HIERONDER
    model_urn = str("<" + uri + ">")
    app.logger.info(f"model_urn {model_urn}")
    query = f"""
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX mb: <http://modellenbibliotheek.belastingdienst.nl/def/mb#>
    PREFIX tlb: <http://modellenbibliotheek.belastingdienst.nl/def/tlb#>
    SELECT ?label ?type
    WHERE {{
    GRAPH {model_urn} {{
    {model_urn} rdfs:label ?label.
    {model_urn} mb:versieVan ?model.
    ?model a ?modeltype.
    FILTER (?modeltype!=mb:Model)
    }}
    OPTIONAL {{
    GRAPH {model_urn} {{
    {model_urn} mb:versieVan ?model.
    ?model a ?modeltype.
    FILTER (?modeltype!=mb:Model)
    }}
    GRAPH <urn:name:types> {{
    ?modeltype rdfs:label ?type.
    }}
    }}
    }} 
    """
    response2 = requests.post(
        config.DATASTORE_ENDPOINT, data={"query": query}, verify=False
    )
    if response2.status_code == 200:
        response_json = str(response2.json())
        # Convert JSON String to Python
        print("Response %s" % response2.status_code)
        results = json.loads(response_json.replace("'", '"'))

        for result in results["results"]["bindings"]:
            print(result["label"]["value"])  # Akten 1.0.0
            version_model = result["label"]["value"]
            print(result["type"]["value"])  # SBM
            type_model = result["type"]["value"]

            filename = type_model + " " + version_model
    response.headers["Content-Disposition"] = f'attachment;filename="{filename}.ttl"'
    return response


@app.route(config.QUERYDIENST_ROUTE, methods=["POST"])
# @check_role_access
@add_melding_manager
def querydienst(melding_manager):
    input_query = request.form["sparql_query"]
    model_uri = request.form.get("model", "").strip()
    query = input_query.replace(f"@{cn.NAMED_GRAPH_URI_PLACEHOLDER}@", model_uri)
    response = requests.post(
        config.DATASTORE_LOOKUP_ENDPOINT, data={"query": query}, verify=False
    )
    if response.status_code == 200:
        # return jsonify(str(response.json()))
        # python object(dictionary) to be dumped
        dict1 = str(response.json())
        # the json file where the output must be stored
        # API_ROOT_DIR = os.path.join(os.getcwd(),  "frontend" + os.sep + "static") # old routing
        API_ROOT_DIR = os.path.join(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "static"
        )  # new path
        file_name = "api.json"
        target_file = API_ROOT_DIR + os.sep + file_name
        out_file = open(f"{target_file}", "w")
        json.dump(dict1, out_file, indent=6)
        out_file.close()
        """
        # old version
        return send_from_directory(API_ROOT_DIR, file_name, as_attachment=True)
        """

        return jsonify(
            {
                "code": response.status_code,
                "resultaat": "SR",
                "opmerking": response.text,
                "sparql_query": query,
                "model": "",
            }
        )

        # curl http://127.0.0.1:5000/static/api.json
    else:
        foutmelding = f"De door u opgegeven query kan niet uitgevoerd worden in SPARQL. Foutmelding: {response.text}"
        melding_manager.meld(ms.ERROR, foutmelding)
        melding_manager.meld(ms.ERROR, response.text, attribuut="sparql_query")
        app.logger.exception(
            f"ERROR: An exception occurred:\n{foutmelding}{''.join(traceback.format_stack())}"
        )

        return jsonify(
            {
                "code": response.status_code,
                "resultaat": ms.ERROR,
                "opmerking": foutmelding,
                "sparql_query": query,
                "model": "",
            }
        )


@app.route(config.FUSEKI_RESTORE)
def fuseki_restore():
    result = subprocess.run(
        ["python", "utils" + os.sep + "fuseki_restore.py"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return jsonify(result.stdout)
    else:
        return jsonify(result.stderr)


#####################################################################################################
# KENNISGEBIEDENREGISTER VULLEN & UPDATEN
#####################################################################################################
@app.route("/kennisgebiedenregister/vullen")
def fill_kennisgebiedenregister():
    result = subprocess.run(
        [
            "python",
            "-c",
            "from frontend.kennisgebiedenregister_creator import KennisgebiedenregisterCreator; kennisgebiedenregister_creator = KennisgebiedenregisterCreator(); kennisgebiedenregister_creator.fill_database();",
        ],
        capture_output=True,
        text=True,
    )
    return jsonify(result.stdout if result.returncode == 0 else result.stderr)


# TODO:
#  ALLE ONDERSTAANDE ROUTES MOET NOG WORDEN TOEGEVOEGD AAN DE CONFIG MAP, ALS DE FUNCTIE GEFINALISEERD IS


@app.route("/modelverzoeken/read", methods=["GET"])
@add_melding_manager
def lees_modelverzoek(melding_manager, table="modelverzoeken"):
    """
    Functie voor het lezen van een modelverzoek uit postgres
    """
    global username
    global usergroep
    primary_key_attributes = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]
    input_key_values = [request.args.get(key) for key in request.args]

    if not input_key_values:
        error_msg = f"No primary_key found in query_params, make sure to go to /modelverzoeken/read/?jira_number=<your_jira_number>&named_graph=<your_named_graph"
        melding_manager.meld(ms.ERROR, error_msg)

        return jsonify({"error": error_msg})

    primary_key_query = (
        f"{primary_key_attributes[0]} LIKE '%{input_key_values[0].split('/')[-1]}%' "
    )

    app.logger.info(primary_key_query)

    def lees_modelverzoek_modelautoriteit():
        # de modelautoriteit mag alles zien
        query = f"SELECT * FROM {table} WHERE {primary_key_query}"

        app.logger.info(
            [
                f"{primary_key_attribute} = '{primary_key_value}'"
                for primary_key_attribute, primary_key_value in zip(
                    primary_key_attributes, input_key_values
                )
            ]
        )
        app.logger.info(query)

        url_suffix_primary_keys = "&".join(
            [
                f"{primary_key_attribute}={primary_key_value}"
                for primary_key_attribute, primary_key_value in zip(
                    primary_key_attributes, input_key_values
                )
            ]
        )

        # modelautoriteit mag alle acties uitvoeren
        py_acties = {
            key: value + url_suffix_primary_keys
            for key, value in routes["py_acties"]["modelverzoek"].items()
        }
        gebruiker_acties = {}
        try:
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            db_cursor.execute(query)
            db_connection.commit()
            result = db_cursor.fetchone()
            column_names = [desc[0] for desc in db_cursor.description]

            app.logger.info(f"db result: {result}")
            app.logger.info(
                f"POSTGRES_MAPPING items: {cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items()}"
            )
            app.logger.info(f"db columns: {column_names}")
            if not result:
                error_msg = "Modelverzoek niet gevonden"
                melding_manager.meld(ms.ERROR, error_msg)
                return (
                    jsonify(
                        {
                            "resultaat": "ERROR",
                            "py_acties": py_acties,
                            "gebruiker_acties": gebruiker_acties,
                            "titel": f"Modelverzoek {input_key_values[0]}",
                            "input_element": "[]",
                        }
                    ),
                    200,
                )

            attribute_list = []
            modelverzoekstatus = json.load(
                open(
                    os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                    + os.sep
                    + "frontend"
                    + os.sep
                    + "ep"
                    + os.sep
                    + "mb-json-options.json"
                )
            )
            modelverzoekstatuslist = modelverzoekstatus[
                "modelVerzoekStatusModelautoriteit"
            ]
            statusmau = modelverzoekstatuslist["options"]
            db_result_dict = dict(zip(column_names, result))
            gebruiker_acties = {
                key: value + url_suffix_primary_keys
                for key, value in routes["gebruiker_acties"]["modelverzoek"].items()
            }

            match db_result_dict["modelverzoek_status"]:
                case "Aangemeld" | "Ingediend":
                    gebruiker_acties.pop("voortzetten")
                case "Uitgesteld":
                    gebruiker_acties.pop("uitstellen")
                case "Ingetrokken":
                    gebruiker_acties.pop("intrekken")
                case "Afgerond":
                    gebruiker_acties.pop("publiceren")
                case _:
                    gebruiker_acties.pop("voortzetten")

            if db_result_dict["request_type"] == "TPV":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["TPV"]
            elif db_result_dict["request_type"] == "FBV":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["FBV"]
            elif db_result_dict["request_type"] == "INR":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["INR"]

            for colname in db_result_dict:
                value = db_result_dict[colname]
                isTimeFormat = False
                if colname == "upload_moment" or colname == "laatste_wijziging":
                    isTimeFormat = True

                attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][colname]
                attribute_value_dict = {"attribuut": colname}

                if attribute_metadata["type"] != "hide":
                    for metadata in attribute_metadata:
                        if attribute_value_dict["attribuut"] == "modelverzoek_status":
                            attribute_metadata["opties"] = statusmau
                            attribute_metadata["opties"][
                                db_result_dict["modelverzoek_status"]
                            ] = db_result_dict["modelverzoek_status"]
                        if metadata == "waarde":
                            if isTimeFormat:
                                if value is not None:
                                    try:
                                        attribute_value_dict["waarde"] = value.strftime(
                                            "%d-%m-%Y %H:%M"
                                        )
                                    except Exception as e:
                                        app.logger.info(
                                            f"datetime waarde van: {metadata} is incorrect, inhoud is: {value}, error is: {e}"
                                        )
                                        attribute_value_dict["waarde"] = ""
                                else:
                                    attribute_value_dict["waarde"] = ""
                            else:
                                attribute_value_dict["waarde"] = (
                                    value if value is not None else ""
                                )
                                if colname == "binary_data":
                                    attribute_value_dict["waarde"] = db_result_dict[
                                        "model_name"
                                    ]
                        else:
                            attribute_value_dict[metadata] = attribute_metadata[
                                metadata
                            ]

                    app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
                    attribute_list.append(attribute_value_dict)
            sorted_attribute_list = sorted(attribute_list, key=lambda x: x["position"])

            db_cursor.close()
            db_connection.close()
            melding_manager.meld()
            return (
                jsonify(
                    {
                        "resultaat": "OK",
                        "py_acties": py_acties,
                        "gebruiker_acties": gebruiker_acties,
                        "titel": f"Modelverzoek {input_key_values[0]}",
                        "input_element": sorted_attribute_list,
                    }
                ),
                200,
            )

        except Exception as e:
            error_msg = f"\nOphalen modelverzoek mislukt.\nDe inhoud van het modelverzoek met URI '{input_key_values[0]}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")

            return (
                jsonify(
                    {
                        "resultaat": "ERROR",
                        "py_acties": py_acties,
                        "gebruiker_acties": gebruiker_acties,
                        "titel": f"Modelverzoek {input_key_values[0]}",
                        "input_element": "[]",
                    }
                ),
                200,
            )

    def lees_modelverzoek_modelleur_and_datasteward():
        # de modelleur en datasteward mogen alleen eigen verzoeken zien
        # en alleen velden aanpassen afhankelijk van de status
        # query = f"SELECT * FROM {table} WHERE {primary_key_query} AND bd_user = '{username}'"
        # query = f"SELECT * FROM {table} WHERE file_extension != '.ttl' AND {primary_key_query} AND bd_user = '{username}'"

        query = f"SELECT * FROM {table} WHERE {primary_key_query}"
        # if above line doesnt work in OTAP, user has to be determined from sso; somewhere around line 157 shows how a user can be determined from sso,

        app.logger.info(
            [
                f"{primary_key_attribute} = '{primary_key_value}'"
                for primary_key_attribute, primary_key_value in zip(
                    primary_key_attributes, input_key_values
                )
            ]
        )
        app.logger.info(query)

        url_suffix_primary_keys = "&".join(
            [
                f"{primary_key_attribute}={primary_key_value}"
                for primary_key_attribute, primary_key_value in zip(
                    primary_key_attributes, input_key_values
                )
            ]
        )
        # modelautoriteit mag alle acties uitvoeren
        py_acties = {
            key: value + url_suffix_primary_keys
            for key, value in routes["py_acties"]["modelverzoek"].items()
        }
        gebruiker_acties = {}
        try:
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            db_cursor.execute(query)
            db_connection.commit()
            result = db_cursor.fetchone()
            column_names = [desc[0] for desc in db_cursor.description]

            app.logger.info(f"db result: {result}")
            app.logger.info(
                f"POSTGRES_MAPPING items: {cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items()}"
            )
            app.logger.info(f"db columns: {column_names}")
            if not result:
                error_msg = f"\nOphalen modelverzoek mislukt.\nDe inhoud van het modelverzoek met URI '{input_key_values[0]}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                melding_manager.meld(ms.ERROR, error_msg)
                app.logger.exception("".join(traceback.format_stack()))
                return (
                    jsonify(
                        {
                            "resultaat": "ERROR",
                            "py_acties": py_acties,
                            "gebruiker_acties": gebruiker_acties,
                            "titel": f"Modelverzoek {input_key_values[0]}",
                            "input_element": "[]",
                        }
                    ),
                    200,
                )

            attribute_list = []
            modelverzoekstatus = json.load(
                open(
                    os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
                    + os.sep
                    + "frontend"
                    + os.sep
                    + "ep"
                    + os.sep
                    + "mb-json-options.json"
                )
            )
            modelverzoekstatuslist = modelverzoekstatus["modelVerzoekStatusIndiener"]
            statusindiener = modelverzoekstatuslist["options"]

            db_result_dict = dict(zip(column_names, result))
            gebruiker_acties = {
                key: value + url_suffix_primary_keys
                for key, value in routes["gebruiker_acties"]["modelverzoek"].items()
            }

            match db_result_dict["modelverzoek_status"]:
                case "Aangemeld" | "Ingediend":
                    del gebruiker_acties["voortzetten"]
                case "Uitgesteld":
                    del gebruiker_acties["uitstellen"]
                case "Ingetrokken":
                    del gebruiker_acties["intrekken"]
                case "Afgerond":
                    del gebruiker_acties["publiceren"]
                case _:
                    del gebruiker_acties["voortzetten"]

            gebruiker_acties.pop(
                "publiceren", None
            )  # indiener mag sowieso niet publiceren

            all_display_only = False
            if db_result_dict["modelverzoek_status"] in (
                "Afgerond",
                "Ingetrokken",
                "Duplicaat",
            ):
                all_display_only = True
                py_acties.clear()

            if db_result_dict["request_type"] == "TPV":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["TPV"]
            elif db_result_dict["request_type"] == "FBV":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["FBV"]
            elif db_result_dict["request_type"] == "INR":
                db_result_dict["request_type"] = cn.mb_json_options["soortVerzoek"][
                    "options"
                ]["INR"]

            for colname in db_result_dict:
                value = db_result_dict[colname]
                isTimeFormat = False

                if colname == "upload_moment":
                    isTimeFormat = True

                attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][colname]
                attribute_value_dict = {"attribuut": colname}

                if attribute_metadata["type"] != "hide":
                    for metadata in attribute_metadata:
                        if attribute_value_dict["attribuut"] == "modelverzoek_status":
                            attribute_metadata["opties"] = statusindiener
                            attribute_metadata["opties"][
                                db_result_dict["modelverzoek_status"]
                            ] = db_result_dict["modelverzoek_status"]
                            attribute_metadata["type"] = "display"
                        if metadata == "waarde":
                            if isTimeFormat:
                                if value is not None:
                                    try:
                                        attribute_value_dict["waarde"] = value.strftime(
                                            "%d-%m-%Y %H:%M"
                                        )
                                    except Exception as e:
                                        app.logger.info(
                                            f"datetime waarde van: {metadata} is incorrect, inhoud is: {value}, error is: {e}"
                                        )
                                        attribute_value_dict["waarde"] = ""
                                else:
                                    attribute_value_dict["waarde"] = ""
                            else:
                                attribute_value_dict["waarde"] = (
                                    value if value is not None else ""
                                )
                                if colname == "binary_data":
                                    attribute_value_dict["waarde"] = db_result_dict[
                                        "model_name"
                                    ]
                        elif metadata == "type" and all_display_only:
                            attribute_value_dict["type"] = "display"
                        else:
                            attribute_value_dict[metadata] = attribute_metadata[
                                metadata
                            ]

                    app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
                    attribute_list.append(attribute_value_dict)

            sorted_attribute_list = sorted(attribute_list, key=lambda x: x["position"])

            db_cursor.close()
            db_connection.close()
            melding_manager.meld()
            return (
                jsonify(
                    {
                        "resultaat": "OK",
                        "py_acties": py_acties,
                        "gebruiker_acties": gebruiker_acties,
                        "titel": f"Modelverzoek {input_key_values[0]}",
                        "input_element": sorted_attribute_list,
                    }
                ),
                200,
            )

        except Exception as e:
            error_msg = f"\nOphalen modelverzoek mislukt.\nDe inhoud van het modelverzoek met URI '{input_key_values[0]}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.exception("".join(traceback.format_stack()))
            return (
                jsonify(
                    {
                        "resultaat": "ERROR",
                        "py_acties": py_acties,
                        "gebruiker_acties": gebruiker_acties,
                        "titel": f"Modelverzoek {input_key_values[0]}",
                        "input_element": "[]",
                    }
                ),
                500,
            )

    if usergroep in ["mod", "das"]:
        return lees_modelverzoek_modelleur_and_datasteward()
    elif usergroep in ["mau", "pow"]:
        return lees_modelverzoek_modelautoriteit()


@app.route("/modelverzoeken/update", methods=["POST"])
@add_melding_manager
def update_modelverzoek(melding_manager, table="modelverzoeken"):
    """
    params:
        note: upload_moment expects date string format in the form of 'DD-MM-YYYY HH:MM' for example: '13-09-2000 18:00'
    """
    titel = "Update modelverzoek"
    # 1. transform ttl
    # 2. delete old_named_graph if new is created
    # 3. update_postgres
    # 4. delete modelverzoek triples + put new modelverzoek triples

    # 1. Transform file into .ttl AND insert this ttl into Fuseki
    def transform_turtle_and_create_model_fuseki(file_content_bytes):
        # (The combination of DELETE+INSERT is equal to an update of the whole graph in Fuseki)

        ## Pre-work for the transformation
        dir_path = tempfile.mkdtemp()

        input_file_path = os.path.join(dir_path, file_name)
        print(input_file_path)
        with open(input_file_path, "wb") as f:
            f.write(file_content_bytes)

            # Transformation
            ## ALSO INSERTS THE NEW .ttl QUAD IN FUSEKI
            try:
                turtle_creator = TurtleCreator(
                    [rf"{input_file_path}"], model_type=upload_data["model_type"]
                )
                turtle_creator.create_turtle()
                print("VDA naam = ", turtle_creator.get_vda_model_name())

            except ValueError as v:
                error_msg = f"{str(v)}"
                melding_manager.meld(ms.ERROR, error_msg)

            except Exception as e:
                app.logger.exception(
                    f"ERROR: An exception occurred:\n{traceback.format_stack()}"
                )
                error_msg = f"Er is iets misgegaan bij de ttl-transformatie van het bestand. Weet je zeker dat je een BMS of Powerdesigner LDM bestand hebt gekozen?"
                melding_manager.meld(ms.ERROR, error_msg)

        return str(turtle_creator.uri), turtle_creator.get_vda_model_name()

    # 2. DELETE current modelverzoeks' model graph in Fuseki
    def delete_model_fuseki(named_graph_to_delete: str):
        try:
            url = config.DATASTORE_ENDPOINT + "/update"

            payload = f"DELETE WHERE {{\n  GRAPH <{named_graph_to_delete}> {{\n    ?s ?p ?o .\n  }}\n}} ;"
            headers = {
                "Content-Type": "application/sparql-update",
            }

            response = requests.request(
                "POST", url, data=payload, headers=headers, verify=False
            )

            if response.status_code != 204:
                # Log every possible detail of the response
                app.logger.info("------ RESPONSE DETAILS ------")

                # 1. Status Code
                app.logger.info(f"Status Code: {response.status_code}")

                # 2. Response Text (Raw text of the response)
                app.logger.info(f"Response Text: {response.text}")

                # 3. Try to log JSON response (if applicable)
                try:
                    json_data = response.json()
                    app.logger.info(f"Response JSON: {json_data}")
                except ValueError:
                    app.logger.info("Response is not in JSON format.")

                # 4. Reason (Textual description of the HTTP response status code)
                app.logger.info(f"Reason: {response.reason}")

                # 5. Request Information (Request object containing details about the request)
                app.logger.info(f"Request URL: {response.request.url}")
                app.logger.info(f"Request Method: {response.request.method}")
                app.logger.info(f"Request Headers: {response.request.headers}")
                app.logger.info(f"Request Body: {response.request.body}")

                # 6. Response Headers (All the headers received in the response)
                app.logger.info(f"Response Headers: {response.headers}")

                # 7. Elapsed Time (Time taken to complete the request)
                app.logger.info(f"Elapsed Time: {response.elapsed}")

                # 8. History (In case of redirects, history will have the list of requests made)
                app.logger.info(f"Response History: {response.history}")

                # 9. Cookies (Any cookies received in the response)
                app.logger.info(f"Response Cookies: {response.cookies}")

                # 10. Encoding (The apparent encoding used by the server for the response)
                app.logger.info(f"Response Encoding: {response.encoding}")

                # 11. Raw Response Content (Binary content, useful for non-text responses)
                app.logger.info(
                    f"Raw Response Content (first 100 bytes): {response.content[:100]}"
                )

                # 12. Check if the response is a redirect
                app.logger.info(f"Is Redirect: {response.is_redirect}")
                return (
                    jsonify(
                        {
                            "resultaat": "ERROR",
                            "error": f"FusekiError: Fuseki failed to delete model graph"
                            f"\nEr is iets misgegaan bij de ttl-transformatie van het bestand. Weet je zeker dat je een BMS of Powerdesigner LDM bestand hebt gekozen?",
                        }
                    ),
                    400,
                )

            else:
                # success_msg = f"Het vorige model met URI '{graph_uri}' is succesvol verwijderd uit de Knowledge Graph."
                # melding_manager.meld(ms.INFO, success_msg)
                app.logger.info(
                    f"Succesfully deleted <{named_graph_to_delete}> in fuseki, returned: {response}"
                )

        except Exception as e:
            error_msg = f"Het huidige model met URI '{named_graph_to_delete}' kon niet verwijderd worden uit de Knowledge Graph."
            app.logger.exception(
                f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
            )
            melding_manager.meld(ms.ERROR, error_msg)

    # 3. Update Postgres
    def update_postgres(model_uri, vda_model_naam):
        # Commented code is for when you run transformation as subprocess
        # render_result = result.stdout

        # vda_model_naam = re.search(r"VDA naam =(.+?)\n", render_result).group(
        #     1).lstrip()  # Removing first two  spaces from string
        # turtle_url = re.search(r"endpoint =(.+?)\n", render_result).group(1)
        # named_graph = turtle_url.replace(config.DATASTORE_ENDPOINT + "?graph=", "")
        # turtle_content = requests.get(turtle_url, verify=False).text.encode("utf-8")
        # TODO: delete comments above if you see this after december 2024

        ic(model_uri)
        ic(config.DATASTORE_ENDPOINT)
        turtle_url = config.DATASTORE_ENDPOINT + "?graph=" + model_uri
        named_graph = turtle_url.replace(config.DATASTORE_ENDPOINT + "?graph=", "")
        turtle_content = requests.get(turtle_url, verify=False).text.encode("utf-8")

        # file_id = uuid.uuid4().hex dont make a new file_id, reuse the old,
        # this should be unique and non-changing primary key throughout the lifetime of a modelverzoej

        ## 4.1 Update Postgres with turtle document
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        with db_cursor:
            for key, _ in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items():
                if key == "binary_data":
                    app.logger.info(f"turtlecontent.type is {type(turtle_content)}")
                    fields.append(f"{key} = %s")
                    values_ttl.append(turtle_content)
                    continue
                if key == "file_extension":
                    fields.append(f"{key} = %s")
                    values_ttl.append(".ttl")
                    continue
                if key == "file_id":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{modelverzoek_uuid4}-ttl")
                    continue
                if key == "model_name":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{model_name}")
                    continue
                if key == "vda_model_name":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{vda_model_naam}")
                    continue
                if key == "named_graph":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{model_uri}")
                    continue
                if key in ("upload_moment", "laatste_wijziging"):
                    continue
                if key in (primary_key_attributes):
                    continue
                if key not in (upload_data):
                    continue

                value = upload_data.get(key)
                app.logger.info(f"{key}: {value}")

                fields.append(f"{key} = %s")
                values_ttl.append(upload_data.get(key))

            # TODO: db_cursor.execute(create_table_query)

            primary_key_query_ttl = f"{primary_key_attributes[0]} LIKE '%%{primary_key_values[0].split('/')[-1]}-ttl%%' "
            query_ttl = f"""
                 UPDATE {table}
                 SET {', '.join(fields)}
                 WHERE {primary_key_query_ttl}
             """
            #        AND bd_user = '{username}'
            app.logger.info(f"query ttl = {query_ttl}")
            app.logger.info(f"values ttl = {values_ttl}")  # remove binary_data for log?
            app.logger.info(
                f"values ttl length = {len(values_ttl)}"
            )  # remove binary_data for log?

            try:
                db_connection = psycopg2.connect(**config.DB_CONFIG)
                db_cursor = db_connection.cursor()
                db_cursor.execute(query_ttl, values_ttl)
                # Check the number of updated rows
                rows_updated = db_cursor.rowcount
                db_connection.commit()
                db_cursor.close()
                db_connection.close()
                app.logger.info(
                    f"updated .ttl in postgres"
                )  # remove binary_data for log?
                if rows_updated == 0:
                    app.logger.info(
                        f"No rows updated with query {query_ttl}, are you sure this is the right uri? {modelverzoek_uri}\n"
                    )
                    error_msg = (
                        f"Uw modelverzoek kon niet worden ingediend."
                        f"Modelverzoek met URI: {primary_key_attributes[0]} kon niet gevonden worden. "
                        f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                    )
                    app.logger.info(error_msg)
                    melding_manager.meld(ms.ERROR.error_msg)
                    return None

            except Exception as e:
                error_msg = f"Uw modelverzoek kon niet worden ingediend. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                app.logger.exception(
                    f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
                )
                melding_manager.meld(ms.ERROR, error_msg)
                app.logger.info(
                    f"Er is iets misgegaan bij het updaten van postgres met query {query_ttl}\n"
                )
                return None

            ## 4.2 Update Postgres with current new original document
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            with db_cursor:
                for key, _ in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items():
                    if key == "binary_data":
                        app.logger.info(
                            f"filecontent.type is {type(file_content_bytes)}"
                        )
                        values.append(file_content_bytes)
                        continue
                    if key == "file_extension":
                        values.append(f"{file_extension}")
                        continue
                    if key == "file_id":
                        values.append(
                            f"{modelverzoek_uuid4}-{file_extension.replace('.', '')}"
                        )
                        continue
                    if key == "model_name":
                        values.append(f"{model_name}")
                        continue
                    if key == "vda_model_name":
                        values.append(f"{vda_model_naam}")
                        continue
                    if key == "named_graph":
                        values.append(f"{model_uri}")
                        continue
                    if key in (primary_key_attributes):
                        continue
                    if key in (
                        "laatst_gewijzigd",
                        "upload_moment",
                        "laatste_wijziging",
                    ):
                        continue
                    if key not in (upload_data):
                        continue

                    value = upload_data.get(key)
                    app.logger.info(f"{key}: {value}")

                    values.append(upload_data.get(key))

                # TODO: db_cursor.execute(create_table_query) optional, todo
                primary_key_query = (
                    f"{primary_key_attributes[0]} LIKE '%%{primary_key_values[0].split('/')[-1]}%%' "
                    f"AND file_extension NOT LIKE '%%ttl%%'"
                )

                query = f"""
                      UPDATE {table}
                      SET {', '.join(fields)}
                      WHERE {primary_key_query}
                  """

                #        AND bd_user = '{username}'
                app.logger.info(f"query = {query}")
                app.logger.info(f"values = {values}")  # remove binary_data for log?
                app.logger.info(
                    f"values count = {len(values)}"
                )  # remove binary_data for log?
                app.logger.info(
                    f"query inputs count = {query.count('%s')}"
                )  # remove binary_data for log?

                try:
                    db_connection = psycopg2.connect(**config.DB_CONFIG)
                    db_cursor = db_connection.cursor()
                    db_cursor.execute(query, values)
                    # Check the number of updated rows
                    rows_updated = db_cursor.rowcount
                    db_connection.commit()
                    db_cursor.close()
                    db_connection.close()
                    if rows_updated == 0:
                        app.logger.info(
                            f"No rows updated with query {query}, are you sure this is the right uri? {modelverzoek_uri}\n"
                        )
                        error_msg = (
                            f"Uw modelverzoek kon niet worden ingediend."
                            f"Modelverzoek met URI: {primary_key_attributes[0]} kon niet gevonden worden. "
                            f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                        )
                        app.logger.info(error_msg)
                        melding_manager.meld(ms.ERROR.error_msg)
                        return None

                except Exception as e:
                    error_msg = f"Uw modelverzoek kon niet worden ingediend. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                    app.logger.exception(
                        f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
                    )
                    melding_manager.meld(ms.ERROR, error_msg)
                    app.logger.info(
                        f"Er is iets misgegaan bij het updaten van postgres met query {query}\n"
                    )
                    return None

    # 4. DELETE AND INSERT new Modelverzoek-triples in Toetsingslogboek
    def delete_and_insert_modelverzoek_into_toetsingslogboek(
        model_uri, modelverzoek_uri
    ):
        # Writing to Toestingslogboek Knowledge Graph
        app.logger.info(f"BEGIN toetsinglogboek")
        model_verzoek_uri = URIRef(
            "http://modellenbibliotheek.belastingdienst.nl/id/verzoek/"
            + str(modelverzoek_uuid4)
        )
        current_toetsingslogboek = requests.get(
            config.TOETSINGSLOGBOEK_ENDPOINT, verify=False
        ).text.encode()
        g = Graph()
        g.parse(current_toetsingslogboek, format="turtle")
        g.bind("tlb", cn.NS_TLB)

        # 1. DELETE OLD MODELVERZOEK-triples
        # Retrieve all triples with the subject `model_verzoek_uri`
        triples_to_remove = g.triples((model_verzoek_uri, None, None))

        # Remove triples except those with predicate `tlb:datumVerzoek`
        for subject, predicate, obj in triples_to_remove:
            if predicate != cn.NS_TLB.datumVerzoek:
                g.remove((subject, predicate, obj))
                app.logger.info(f"Removed triple: {subject}, {predicate}, {obj}")
            else:
                app.logger.info(
                    f"Skipped triple with predicate tlb:datumVerzoek: {subject}, {predicate}, {obj}"
                )

        app.logger.info(f"Successfully removed selected triples for {modelverzoek_uri}")

        # 2. ADD NEW Modelverzoek-triples
        g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Verzoek))

        if (
            upload_data["request_type"] == "TPV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["TPV"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Publicatieverzoek))
        elif (
            upload_data["request_type"] == "FBV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["FBV"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Feedbackverzoek))
        elif (
            upload_data["request_type"] == "INR"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["INR"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.InterneReview))

        g.add((model_verzoek_uri, RDFS.label, Literal(upload_data["title_request"])))

        g.add((model_verzoek_uri, cn.NS_TLB.betreft, URIRef(model_uri)))
        # bij een update blijft datum hetzelfde
        # g.add(
        #     (
        #         model_verzoek_uri,
        #         sbm_cn.NS_TLB.datumVerzoek,
        #         Literal(datetime.now().date(), datatype=XSD.date),
        #     )
        # )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_TLB.ingediendDoor,
                URIRef(
                    "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                    + upload_data[afn.BDUSER]
                ),
            )
        )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_MB.code,
                Literal(upload_data["jira_number"]),
            )
        )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_TLB.jiraLink,
                URIRef(
                    "https://jira.belastingdienst.nl/servicedesk/customer/portal/78/"
                    + upload_data["jira_number"]
                ),
            )
        )

        if "extra_modellers" in upload_data and upload_data["extra_modellers"] != "":
            betrokken_personen = re.split(r"[,\s;]+", upload_data["extra_modellers"])
            for persoon in betrokken_personen:
                g.add(
                    (
                        model_verzoek_uri,
                        cn.NS_TLB.betrokken,
                        URIRef(
                            "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                            + persoon
                        ),
                    )
                )

        match upload_data["modelverzoek_status"]:
            case "Aangemeld":
                new_status_cn = cn.NS_TLB.Aangemeld
            case "Ingediend":
                new_status_cn = cn.NS_TLB.Ingediend
            case "Uitgesteld":
                new_status_cn = cn.NS_TLB.Uitgesteld
            case "Onderhanden":
                new_status_cn = cn.NS_TLB.Onderhanden
            case "Wachtend":
                new_status_cn = cn.NS_TLB.Wachtend
            case "WachtOpAcceptatie":
                new_status_cn = cn.NS_TLB.WachtOpAcceptatie
            case "Afgerond":
                new_status_cn = cn.NS_TLB.Afgerond
            case "Duplicaat":
                new_status_cn = cn.NS_TLB.Duplicaat
            case "Ingetrokken":
                new_status_cn = cn.NS_TLB.Ingetrokken
            case _:
                modelverzoek_status_inhoud = upload_data["modelverzoek_status"]
                error_msg = (
                    f"modelverzoekstatus {modelverzoek_status_inhoud} niet bekend"
                )
                app.logger.info(error_msg)
                melding_manager.meld(ms.ERROR, error_msg)

        g.add((model_verzoek_uri, cn.NS_TLB.status, new_status_cn))

        try:
            with open("a.txt", "wb+") as f:
                f.write(g.serialize(format="turtle").encode("utf-8"))
            app.logger.info(
                f'hier staat de quad modelverzoek {g.serialize(format="turtle")}'
            )
            response = requests.put(
                config.TOETSINGSLOGBOEK_ENDPOINT,
                data=g.serialize(format="turtle").encode("utf-8"),
                headers=cn.turtle_headers,
                verify=False,
            )
            app.logger.info(f"fuseki response status code {response.status_code}")

        except Exception as e:
            error_msg = f"Het updaten van Fuseki is niet gelukt voor modelverzoek {modelverzoek_uuid4}"
            app.logger.exception(
                f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
            )
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.info(error_msg)
            return None

    def get_current_modelverzoek_postgres(modelverzoek_uuid4: str):
        primary_key_query = (
            f"{primary_key_attributes[0]} LIKE '%%{modelverzoek_uuid4}%%' "
        )
        query = f"SELECT * FROM {table} WHERE {primary_key_query} AND file_extension NOT LIKE '%ttl%';"
        #        AND bd_user = '{username}'
        app.logger.info(f"query = {query}")

        try:
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            db_cursor.execute(query)
            db_connection.commit()
            # Check the number of updated rows
            result = db_cursor.fetchone()
            column_names = [desc[0] for desc in db_cursor.description]

            app.logger.info(f"db result: {column_names}")

            db_cursor.close()
            db_connection.close()
            app.logger.info(
                f"got original document from postgres"
            )  # remove binary_data for log?
            return dict(zip(column_names, result))

        except Exception as e:
            error_msg = f"\nHet updaten van uw modelverzoek is niet gelukt.\nDe inhoud van het modelverzoek met URI '{modelverzoek_uuid4}' kon niet worden opgehaald. Controleer of het originele modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            app.logger.exception(
                f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
            )
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.info(error_msg)
            return None

    fields = []
    values_ttl = []
    values = []

    primary_key_attributes = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]
    primary_key_values = [request.args.get(key) for key in request.args]
    upload_data = dict(request.form)
    if not primary_key_values:
        error_msg = f"No primary_key found in query_params nor the url"
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": ms.ERROR,
                    "error": error_msg,
                }
            ),
            400,
        )

    modelverzoek_uri = primary_key_values[0]
    modelverzoek_uuid4 = modelverzoek_uri.split("/")[-1]
    old_model_uri = str(primary_key_values[1])

    if "request_type" in upload_data:
        if (
            upload_data["request_type"] == "TPV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["TPV"]
        ):
            upload_data["request_type"] = "TPV"
        elif (
            upload_data["request_type"] == "FBV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["FBV"]
        ):
            upload_data["request_type"] = "FBV"
        elif (
            upload_data["request_type"] == "INR"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["INR"]
        ):
            upload_data["request_type"] = "INR"

    if (
        "binary_data" in request.files
        and request.files["binary_data"].filename != ""
        and request.files["binary_data"] != "undefined"
    ):
        postgres_original_file_data = get_current_modelverzoek_postgres(
            modelverzoek_uuid4
        )
        for column in postgres_original_file_data:
            if column not in upload_data:
                upload_data[column] = postgres_original_file_data[column]
        app.logger.info(
            "binary_data in request.files and request.files['binary_data'].filename != empty"
        )
        app.logger.info("Taking the FILE UPDATE route")

        # Retrieve the file from the request
        file = request.files["binary_data"]

        file_name = file.filename.replace(" ", "_")
        file_extension = "." + file_name.split(".")[-1:][0]
        model_name = file_name.replace(file_extension, "")

        file_content_bytes = file.read()
        if (not file_extension.startswith(".xl")) and (
            file_extension != cn.LDM_EXTENSION
        ):  # we only accept .xl* and .ldm file_extensions
            error_msg = f"Verkeerd bestandstype geselecteerd. Kies een BMS (.xl*) of Powerdesigner LDM (.ldm) bestand."
            melding_manager.meld(ms.ERROR, error_msg)
            return (
                jsonify(
                    {
                        "resultaat": ms.ERROR,
                        "error": error_msg,
                    }
                ),
                400,
            )

    else:
        app.logger.info("taking file from postgres...")
        upload_data.pop("binary_data", None)  # If 'd' is not found, return None

        postgres_original_file_data = get_current_modelverzoek_postgres(
            modelverzoek_uuid4
        )
        for column in postgres_original_file_data:
            if column not in upload_data:
                upload_data[column] = postgres_original_file_data[column]
        upload_data["jira_link"] = (
            "https://jira.belastingdienst.nl/servicedesk/customer/portal/78/"
            + upload_data["jira_number"]
        )

        app.logger.info(f'file_content type = {type(upload_data["binary_data"])}')
        app.logger.info(f'file_content = {upload_data["binary_data"]}')

        file_content_bytes = upload_data["binary_data"]

        model_name = upload_data["model_name"]
        file_extension = upload_data["file_extension"]
        file_name = model_name + file_extension

    # Check if there has been an error in the function up until this point. If error: stop function and return the error
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Wijziging mislukt, gegevens kloppen (waarschijnlijk) niet.",
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # 1. transform ttl
    # 2. delete old_named_graph if new is created
    # 3. update_postgres
    # 4. delete modelverzoek triples + put new modelverzoek triples

    # 1. Transform file into .ttl AND insert this ttl into Fuseki

    (model_uri, vda_model_naam) = transform_turtle_and_create_model_fuseki(
        file_content_bytes=file_content_bytes
    )

    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Er is iets misgegaan bij de ttl-transformatie van het bestand. Weet je zeker dat je een valide BMS of Powerdesigner LDM bestand hebt gekozen?",

        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # 2. DELETE current modelverzoeks' model graph in Fuseki if new one is created
    if old_model_uri != model_uri:
        delete_model_fuseki(named_graph_to_delete=old_model_uri)

    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Transformatie gelukt, maar verwijderen van het model graph <{old_model_uri}> in fuseki niet."
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # 3. Update Postgres
    update_postgres(model_uri=model_uri, vda_model_naam=vda_model_naam)
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Transformatie gelukt, maar updaten van SQL-database niet"

        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # (deprecated) DELETE current modelverzoek-triples in Fuseki toetsingslogboek
    # graph_uri = delete_modelverzoek_fuseki(modelverzoek_uri=modelverzoek_uri)
    # 4. DELETE AND INSERT new Modelverzoek-triples in Toetsingslogboek
    delete_and_insert_modelverzoek_into_toetsingslogboek(
        model_uri=model_uri, modelverzoek_uri=modelverzoek_uri
    )
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Transformatie en updaten postgres gelukt, maar delete en insert van modelverzoek-triples in toetsingslogboek niet"

        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    else:
        app.logger.info(
            f"Nieuw modelverzoek inclusief model is succesvol geupdate in fuseki en SQL"
        )
        success_message = (f"Wijziging doorgevoerd",)
        melding_manager.meld(ms.SUCCESS, success_message)

        return (
            jsonify(
                {
                    "resultaat": f"OK",
                }
            ),
            200,
        )


@app.route("/modelverzoeken/actie/<update>", methods=["POST"])
@add_melding_manager
def update_status_modelverzoek(melding_manager, update: str):
    """
    params:
        note: upload_moment expects date string format in the form of 'DD-MM-YYYY HH:MM' for example: '13-09-2000 18:00'
    """
    titel = "Wijzigen status modelverzoek"
    update = update.lower().capitalize()
    if update == "Voortzetten":
        new_status = "Ingediend"
    elif update == "Uitstellen":
        new_status = "Uitgesteld"
    elif update == "Intrekken":
        new_status = "Ingetrokken"
    elif update == "Publiceren":
        new_status = "Afgerond"
    upload_data = {
        "modelverzoek_status": cn.mb_json_options["modelVerzoekStatus"]["options"][
            new_status
        ]
    }

    # 1. Transform file into .ttl AND insert this ttl into Fuseki
    def get_model_uri_from_modelverzoek_uri(modelverzoek_uri: str):
        try:
            model_uri = lookup_model_uri_from_modelverzoek_uri(
                lookup_endpoint=config.DATASTORE_ENDPOINT,
                lookup_modelverzoek_uri=modelverzoek_uri,
            )

        except ValueError as v:
            app.logger.exception(
                f"ERROR: An exception occurred:\n{v}\n{traceback.format_stack()}"
            )
            error_msg = f"\nOphalen modelverzoek mislukt.\nDe inhoud van het modelverzoek met URI '{modelverzoek_uri}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            melding_manager.meld(ms.ERROR, error_msg)

        except Exception as e:
            error_msg = f"\nOphalen modelverzoek mislukt.\nDe inhoud van het modelverzoek met URI '{modelverzoek_uri}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")

        return model_uri

    # 2. DELETE current modelverzoeks' model graph in Fuseki
    def delete_model_fuseki(named_graph_to_delete: str):
        try:
            url = config.DATASTORE_ENDPOINT + "/update"

            payload = f"DELETE WHERE {{\n  GRAPH <{named_graph_to_delete}> {{\n    ?s ?p ?o .\n  }}\n}} ;"
            headers = {
                "Content-Type": "application/sparql-update",
            }

            response = requests.request(
                "POST", url, data=payload, headers=headers, verify=False
            )

            if response.status_code != 204:
                error_msg = (
                    f"FusekiError: Fuseki failed to delete model graph"
                    f"\nEr is iets misgegaan bij het verwijderen van het oude model met URI {named_graph_to_delete}."
                    f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                )
                app.logger.info(error_msg)

                melding_manager.meld(ms.ERROR, error_msg)
                return None
            else:
                app.logger.info(
                    f"Succesfully deleted <{named_graph_to_delete}> in fuseki, returned: {response}"
                )

        except Exception as e:
            error_msg = (
                f"FusekiError: Fuseki failed to delete model graph"
                f"\nEr is iets misgegaan bij het verwijderen van het oude model met URI {named_graph_to_delete}."
                f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            )

            app.logger.info(f"{e}: {traceback.format_exc()}")
            melding_manager.meld(ms.ERROR, error_msg)

            return None

    # 3. Update Postgres
    def update_postgres(model_uri):
        ic(model_uri)
        ic(config.DATASTORE_ENDPOINT)
        turtle_url = config.DATASTORE_ENDPOINT + "?graph=" + model_uri
        named_graph = turtle_url.replace(config.DATASTORE_ENDPOINT + "?graph=", "")
        turtle_content = requests.get(turtle_url, verify=False).text.encode("utf-8")

        # file_id = uuid.uuid4().hex dont make a new file_id, reuse the old,
        # this should be unique and non-changing primary key throughout the lifetime of a modelverzoek

        ## 4.1 Update Postgres with turtle document
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        with db_cursor:
            for key, _ in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items():
                if key == "binary_data":
                    app.logger.info(f"turtlecontent.type is {type(turtle_content)}")
                    fields.append(f"{key} = %s")
                    values_ttl.append(turtle_content)
                    continue
                if key == "file_extension":
                    fields.append(f"{key} = %s")
                    values_ttl.append(".ttl")
                    continue
                if key == "file_id":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{modelverzoek_uuid4}-ttl")
                    continue
                if key == "model_name":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{model_name}")
                    continue
                if key == "named_graph":
                    fields.append(f"{key} = %s")
                    values_ttl.append(f"{model_uri}")
                    continue
                if key in ("upload_moment", "laatste_wijziging"):
                    continue
                if key in (primary_key_attributes):
                    continue
                if key not in (upload_data):
                    continue

                value = upload_data.get(key)
                app.logger.info(f"{key}: {value}")

                fields.append(f"{key} = %s")
                values_ttl.append(upload_data.get(key))

            # TODO: db_cursor.execute(create_table_query)

            primary_key_query_ttl = f"{primary_key_attributes[0]} LIKE '%%{primary_key_values[0].split('/')[-1]}-ttl%%' "
            query_ttl = f"""
                  UPDATE {table}
                  SET {', '.join(fields)}
                  WHERE {primary_key_query_ttl}
              """
            #        AND bd_user = '{username}'
            app.logger.info(f"query = {query_ttl}")
            app.logger.info(f"values = {values_ttl}")  # remove binary_data for log?
            app.logger.info(
                f"values length = {len(values_ttl)}"
            )  # remove binary_data for log?

            try:
                db_connection = psycopg2.connect(**config.DB_CONFIG)
                db_cursor = db_connection.cursor()
                db_cursor.execute(query_ttl, values_ttl)
                # Check the number of updated rows
                rows_updated = db_cursor.rowcount
                db_connection.commit()
                db_cursor.close()
                db_connection.close()
                app.logger.info(
                    f"updated .ttl in postgres"
                )  # remove binary_data for log?
                if rows_updated == 0:
                    app.logger.info(
                        f"No rows updated with query {query_ttl}, are you sure this is the right uri? {modelverzoek_uri}\n"
                    )
                    error_msg = (
                        f"Uw modelverzoek kon niet worden ingediend."
                        f"Modelverzoek met URI: {primary_key_attributes[0]} kon niet gevonden worden. "
                        f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                    )
                    app.logger.info(error_msg)
                    melding_manager.meld(ms.ERROR.error_msg)
                    return None

            except Exception as e:
                error_msg = f"Uw modelverzoek kon niet worden ingediend. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                app.logger.exception(
                    f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
                )
                melding_manager.meld(ms.ERROR, error_msg)
                app.logger.info(
                    f"Er is iets misgegaan bij het updaten van postgres met query {query_ttl}\n"
                )
                return None

            ## 4.2 Update Postgres with current new original document
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            with db_cursor:
                for key, _ in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items():
                    if key == "binary_data":
                        app.logger.info(
                            f"filecontent.type is {type(file_content_bytes)}"
                        )
                        values.append(file_content_bytes)
                        continue
                    if key == "file_extension":
                        values.append(f"{file_extension}")
                        continue
                    if key == "file_id":
                        values.append(
                            f"{modelverzoek_uuid4}-{file_extension.replace('.', '')}"
                        )
                        continue
                    if key == "model_name":
                        values.append(f"{model_name}")
                        continue
                    if key == "named_graph":
                        values.append(f"{model_uri}")
                        continue
                    if key in (primary_key_attributes):
                        continue
                    if key in (
                        "laatst_gewijzigd",
                        "upload_moment",
                        "laatste_wijziging",
                    ):
                        continue
                    if key not in (upload_data):
                        continue

                    value = upload_data.get(key)
                    app.logger.info(f"{key}: {value}")

                    values.append(upload_data.get(key))

                # TODO: db_cursor.execute(create_table_query) optional, todo
                primary_key_query = (
                    f"{primary_key_attributes[0]} LIKE '%%{primary_key_values[0].split('/')[-1]}%%' "
                    f"AND file_extension NOT LIKE '%%ttl%%'"
                )

                query = f"""
                       UPDATE {table}
                       SET {', '.join(fields)}
                       WHERE {primary_key_query}
                   """

                #        AND bd_user = '{username}'
                app.logger.info(f"query = {query}")
                # app.logger.info(f"values = {values}")  # remove binary_data for log?
                app.logger.info(
                    f"values count = {len(values)}"
                )  # remove binary_data for log?
                app.logger.info(
                    f"query inputs count = {query.count('%s')}"
                )  # remove binary_data for log?

                try:
                    db_connection = psycopg2.connect(**config.DB_CONFIG)
                    db_cursor = db_connection.cursor()
                    db_cursor.execute(query, values)
                    # Check the number of updated rows
                    rows_updated = db_cursor.rowcount
                    db_connection.commit()
                    db_cursor.close()
                    db_connection.close()
                    if rows_updated == 0:
                        app.logger.info(
                            f"No rows updated with query {query}, are you sure this is the right uri? {modelverzoek_uri}\n"
                        )
                        error_msg = (
                            f"Uw modelverzoek kon niet worden ingediend."
                            f"Modelverzoek met URI: {primary_key_attributes[0]} kon niet gevonden worden. "
                            f"Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                        )
                        app.logger.info(error_msg)
                        melding_manager.meld(ms.ERROR.error_msg)
                        return None

                except Exception as e:
                    error_msg = f"Uw modelverzoek kon niet worden ingediend. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
                    app.logger.exception(
                        f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
                    )
                    melding_manager.meld(ms.ERROR, error_msg)
                    app.logger.info(
                        f"Er is iets misgegaan bij het updaten van postgres met query {query}\n"
                    )
                    return None

    # 4. DELETE AND INSERT new Modelverzoek-triples in Toetsingslogboek
    def delete_and_insert_modelverzoek_into_toetsingslogboek(
        model_uri, modelverzoek_uri
    ):
        # Writing to Toestingslogboek Knowledge Graph
        app.logger.info(f"BEGIN toetsinglogboek")
        model_verzoek_uri = URIRef(
            "http://modellenbibliotheek.belastingdienst.nl/id/verzoek/"
            + str(modelverzoek_uuid4)
        )
        current_toetsingslogboek = requests.get(
            config.TOETSINGSLOGBOEK_ENDPOINT, verify=False
        ).text.encode()
        g = Graph()
        g.parse(current_toetsingslogboek, format="turtle")
        g.bind("tlb", cn.NS_TLB)

        # 1. DELETE OLD MODELVERZOEK-triples
        # Retrieve all triples with the model_uri `model_verzoek_uri`
        triples_to_remove = g.triples((model_verzoek_uri, None, None))

        # Remove triples except those with predicate `tlb:datumVerzoek`
        for subject, predicate, obj in triples_to_remove:
            if predicate != cn.NS_TLB.datumVerzoek:
                g.remove((subject, predicate, obj))
                app.logger.info(f"Removed triple: {subject}, {predicate}, {obj}")
            else:
                app.logger.info(
                    f"Skipped triple with predicate tlb:datumVerzoek: {subject}, {predicate}, {obj}"
                )

        app.logger.info(f"Successfully removed selected triples for {modelverzoek_uri}")

        # 2. ADD NEW Modelverzoek-triples
        g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Verzoek))

        if (
            upload_data["request_type"] == "TPV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["TPV"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Publicatieverzoek))
        elif (
            upload_data["request_type"] == "FBV"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["FBV"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.Feedbackverzoek))
        elif (
            upload_data["request_type"] == "INR"
            or upload_data["request_type"]
            == cn.mb_json_options["soortVerzoek"]["options"]["INR"]
        ):
            g.add((model_verzoek_uri, RDF.type, cn.NS_TLB.InterneReview))

        g.add((model_verzoek_uri, RDFS.label, Literal(upload_data["title_request"])))

        g.add((model_verzoek_uri, cn.NS_TLB.betreft, URIRef(model_uri)))
        # bij een update blijft datum hetzelfde
        # g.add(
        #     (
        #         model_verzoek_uri,
        #         sbm_cn.NS_TLB.datumVerzoek,
        #         Literal(datetime.now().date(), datatype=XSD.date),
        #     )
        # )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_TLB.ingediendDoor,
                URIRef(
                    "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                    + upload_data[afn.BDUSER]
                ),
            )
        )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_MB.code,
                Literal(upload_data["jira_number"]),
            )
        )
        g.add(
            (
                model_verzoek_uri,
                cn.NS_TLB.jiraLink,
                URIRef(
                    "https://jira.belastingdienst.nl/servicedesk/customer/portal/78/"
                    + upload_data["jira_number"]
                ),
            )
        )

        if "extra_modellers" in upload_data and upload_data["extra_modellers"] != "":
            betrokken_personen = re.split(r"[,\s;]+", upload_data["extra_modellers"])
            for persoon in betrokken_personen:
                g.add(
                    (
                        model_verzoek_uri,
                        cn.NS_TLB.betrokken,
                        URIRef(
                            "http://modellenbibliotheek.belastingdienst.nl/id/medewerker/"
                            + persoon
                        ),
                    )
                )

        match upload_data["modelverzoek_status"]:
            case "Aangemeld":
                new_status_cn = cn.NS_TLB.Aangemeld
            case "Ingediend":
                new_status_cn = cn.NS_TLB.Ingediend
            case "Uitgesteld":
                new_status_cn = cn.NS_TLB.Uitgesteld
            case "Onderhanden":
                new_status_cn = cn.NS_TLB.Onderhanden
            case "Wachtend":
                new_status_cn = cn.NS_TLB.Wachtend
            case "WachtOpAcceptatie":
                new_status_cn = cn.NS_TLB.WachtOpAcceptatie
            case "Afgerond":
                new_status_cn = cn.NS_TLB.Afgerond
            case "Duplicaat":
                new_status_cn = cn.NS_TLB.Duplicaat
            case "Ingetrokken":
                new_status_cn = cn.NS_TLB.Ingetrokken
            case _:
                modelverzoek_status_inhoud = upload_data["modelverzoek_status"]
                error_msg = (
                    f"modelverzoek status '{modelverzoek_status_inhoud}' niet bekend"
                )
                app.logger.info(error_msg)
                melding_manager.meld(ms.ERROR, error_msg)
                return None
        g.add((model_verzoek_uri, cn.NS_TLB.status, new_status_cn))

        try:
            with open("a.txt", "wb+") as f:
                f.write(g.serialize(format="turtle").encode("utf-8"))
            app.logger.info(
                f'hier staat de quad modelverzoek {g.serialize(format="turtle")}'
            )
            response = requests.put(
                config.TOETSINGSLOGBOEK_ENDPOINT,
                data=g.serialize(format="turtle").encode("utf-8"),
                headers=cn.turtle_headers,
                verify=False,
            )
            app.logger.info(f"fuseki response status code {response.status_code}")
        except Exception as e:
            app.logger.exception(
                f"ERROR: An exception occurred:\n{traceback.format_stack()}"
            )

            return (
                jsonify(
                    {
                        "resultaat": "ERROR",
                        "error": f"Het updaten van Fuseki is niet gelukt voor modelverzoek {modelverzoek_uuid4}",
                    }
                ),
                400,
            )

    def update_modelstatus_naar_gepubliceerd(model_uri: str):
        url_update = config.DATASTORE_ENDPOINT + "/update"
        url_query = config.DATASTORE_ENDPOINT + "/query"
        # Define model_uri and predicates
        status_predicate = cn.NS_MB.status
        version_predicate = cn.NS_MB.versienummer
        date_predicate = cn.NS_MB.versiedatum
        release_date_predicate = cn.NS_MB.releaseDatum
        new_status = cn.NS_MB.Gepubliceerd
        publicatie_van_predicate = cn.NS_MB.publicatieVan

        # SPARQL-query om alle gerelateerde data op te halen
        query_old_data = f"""
        SELECT * WHERE {{
          GRAPH <{model_uri}> {{
            <{model_uri}> ?p ?oldData . 
          }}
        }}
        """

        response = requests.post(
            config.DATASTORE_ENDPOINT, data={"query": query_old_data}, verify=False
        )

        if response.status_code != 200:
            error_msg = (
                f"{update} van modelverzoek '{upload_data['title_request']}' met model '{upload_data['vda_model_name']}' is niet gelukt. Controleer of het model met URI '{model_uri}' correct is.",
            )
            app.logger.info(error_msg)
            melding_manager.meld(ms.ERROR, error_msg)

        # Parse de JSON-response
        results = response.json()["results"]["bindings"]

        # Variabelen voor oude data
        old_version_full = None
        old_date = None
        old_status = None

        # Itereer over de resultaten en haal de relevante waarden op
        for result in results:
            predicate = result["p"]["value"]
            value = result["oldData"]["value"]

            if "versienummer" in predicate:
                old_version_full = value
            elif "versiedatum" in predicate:
                old_date = value
            elif "status" in predicate:
                old_status = value

        # Zorg ervoor dat de essentiële variabelen zijn opgehaald
        if not old_version_full or not old_date or not old_status:
            error_msg = (
                f"{update} van modelverzoek '{upload_data['title_request']}' met model '{upload_data['vda_model_name']}' is niet gelukt. Controleer of het model met URI '{model_uri}' correct is.",
            )
            app.logger.info(error_msg)
            melding_manager.meld(ms.ERROR, error_msg)

        # Extract first part of the version
        old_version = old_version_full[
            : (
                [char.isdigit() or char == "." for char in old_version_full] + [False]
            ).index(False)
        ]

        print(" old version ", old_version)
        # old_version = old_version_full.split(" ")[0]
        # Get current date
        current_date = datetime.today().strftime("%Y-%m-%d")
        ic(model_uri)
        # DELETE old status
        delete_query = f"""
            DELETE {{
              GRAPH <{model_uri}> {{
                <{model_uri}> <{version_predicate}> ?old_version_full .
                <{model_uri}> <{date_predicate}> ?old_date .
                <{model_uri}> <{status_predicate}> ?old_status . 
              }}
            }}
            WHERE {{
              GRAPH <{model_uri}> {{
                OPTIONAL {{ <{model_uri}> <{version_predicate}> ?old_version_full . }}
                OPTIONAL {{ <{model_uri}> <{date_predicate}> ?old_date . }}
                OPTIONAL {{ <{model_uri}> <{status_predicate}> ?old_status . }}
              }}
            }}
        """
        ic(delete_query)
        requests.post(
            url_update,
            data=delete_query,
            headers={"Content-Type": "application/sparql-update"},
            verify=False,
        )

        # INSERT new status, release date, and publicatieVan
        insert_query = f"""
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        INSERT DATA {{
          GRAPH <{model_uri}> {{
            <{model_uri}> <{version_predicate}> "{old_version}" .
            <{model_uri}> <{status_predicate}> <{new_status}> .
            <{model_uri}> <{date_predicate}> "{old_date}"^^xsd:date .
            <{model_uri}> <{release_date_predicate}> "{current_date}"^^xsd:date .
            <{model_uri}> <{publicatie_van_predicate}> [
              <{version_predicate}> "{old_version_full}";
              <{date_predicate}> "{old_date}"^^xsd:date;
              <{status_predicate}> <{old_status}>;
            ] .
          }}
        }}
        """

        response = requests.post(
            url_update,
            data=insert_query,
            headers={"Content-Type": "application/sparql-update"},
            verify=False,
        )

        if response.status_code == 204:
            success_message = (
                f"Het model '{upload_data['vda_model_name']}' is gepubliceerd!",
            )
            app.logger.info(success_message)
            melding_manager.meld(ms.SUCCESS, success_message)
        else:
            error_msg = (
                f"{update} van modelverzoek '{upload_data['title_request']}' met model '{upload_data['vda_model_name']}' is niet gelukt.",
            )
            app.logger.info(error_msg)
            melding_manager.meld(ms.ERROR, error_msg)

        # PERFORM NABEWERKING PUBLICATIE QUERIES
        match (upload_data["model_type"]):
            case "SBM":
                fs.execute_procedure(
                    fs.queries.LINK_SBM_MODELVERSIE_INKOMEND, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_SBMS_TO_SBM_INKOMEND, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_SBM_TO_SBMS_UITGAAND, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIMS_TO_SBM_ANDER_KGB_BEGRIP,
                    modelversie_uri=model_uri,
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIMS_TO_SBM_EIGEN_KGB_BEGRIP,
                    modelversie_uri=model_uri,
                )
            case "CIM":
                fs.execute_procedure(
                    fs.queries.LINK_CIM_UITGAAND_MODELVERSIE, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIM_INKOMEND_MODELVERSIE, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIM_TO_SBM_EIGEN_KGB_BEGRIP,
                    modelversie_uri=model_uri,
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIM_TO_SBMS_ANDER_KGB_BEGRIP,
                    modelversie_uri=model_uri,
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIM_TO_CIMS_UITGAAND, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.LINK_CIMS_TO_CIM_INKOMEND, modelversie_uri=model_uri
                )
                fs.execute_procedure(
                    fs.queries.VERWIJDER_SHORTCUTS_IN_CIM, modelversie_uri=model_uri
                )

    def get_current_modelverzoek_postgres(modelverzoek_uuid4: str):
        primary_key_query = (
            f"{primary_key_attributes[0]} LIKE '%%{modelverzoek_uuid4}%%' "
        )
        query = f"SELECT * FROM {table} WHERE {primary_key_query} AND file_extension NOT LIKE '%ttl%';"
        #        AND bd_user = '{username}'
        app.logger.info(f"query = {query}")

        try:
            db_connection = psycopg2.connect(**config.DB_CONFIG)
            db_cursor = db_connection.cursor()
            db_cursor.execute(query)
            db_connection.commit()
            # Check the number of updated rows
            result = db_cursor.fetchone()
            column_names = [desc[0] for desc in db_cursor.description]

            app.logger.info(f"db result: {column_names}")

            db_cursor.close()
            db_connection.close()
            app.logger.info(
                f"got original document from postgres"
            )  # remove binary_data for log?
            return dict(zip(column_names, result))

        except Exception as e:
            error_msg = f"\nHet updaten van uw modelverzoek is niet gelukt.\nDe inhoud van het modelverzoek met URI '{modelverzoek_uuid4}' kon niet worden opgehaald. Controleer of het originele modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
            app.logger.exception(
                f"ERROR: An exception occurred:{error_msg}\n{e}\n{traceback.format_stack()}"
            )
            melding_manager.meld(ms.ERROR, error_msg)
            app.logger.info(error_msg)
            return None

    table = "modelverzoeken"
    fields = []
    values_ttl = []
    values = []

    primary_key_attributes = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]
    primary_key_values = [request.args.get(key) for key in request.args]

    modelverzoek_uri = primary_key_values[0]
    modelverzoek_uuid4 = modelverzoek_uri.split("/")[-1]
    old_model_uri = str(primary_key_values[1])

    app.logger.info("Taking the FILE UPDATE route")

    if not primary_key_values:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "error": f"No primary_key found in query_params nor the url_update",
                }
            ),
            200,
        )

    app.logger.info("taking file from postgres...")
    upload_data.pop("binary_data", None)  # If 'd' is not found, return None

    postgres_original_file_data = get_current_modelverzoek_postgres(modelverzoek_uuid4)
    for column in postgres_original_file_data:
        if column not in upload_data:
            upload_data[column] = postgres_original_file_data[column]
    upload_data["jira_link"] = (
        "https://jira.belastingdienst.nl/servicedesk/customer/portal/78/"
        + upload_data["jira_number"]
    )

    app.logger.info(f'file_content_type = {type(upload_data["binary_data"])}')
    app.logger.info(f'file_content = {upload_data["binary_data"]}')

    file_content_bytes = upload_data["binary_data"].tobytes()

    model_name = upload_data["model_name"]
    file_extension = upload_data["file_extension"]
    file_name = model_name + file_extension

    # Check if there has been an error in the function up until this point. If error: stop function and return the error
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # 1. transform ttl
    # 2. delete old_named_graph if new is created
    # 3. update_postgres
    # 4. delete modelverzoek triples + put new modelverzoek triples

    # 1. Transform file into .ttl AND insert this ttl into Fuseki
    model_uri = get_model_uri_from_modelverzoek_uri(modelverzoek_uri=modelverzoek_uri)
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # 1.2. update modelstatus naar gepubliceerd
    if update == "Publiceren":
        update_modelstatus_naar_gepubliceerd(model_uri=model_uri)
        error_melding = [
            melding for melding in melding_manager if melding.soort == ms.ERROR
        ]
        if error_melding:
            resultaat = ms.ERROR
            return (
                jsonify(
                    {
                        "resultaat": resultaat,
                        "titel": titel,
                    }
                ),
                200,
            )

    # 2. DELETE current modelverzoeks' model graph in Fuseki if new one is created
    if old_model_uri != model_uri:
        delete_model_fuseki(named_graph_to_delete=old_model_uri)
        error_melding = [
            melding for melding in melding_manager if melding.soort == ms.ERROR
        ]
        if error_melding:
            resultaat = ms.ERROR
            return (
                jsonify(
                    {
                        "resultaat": resultaat,
                        "titel": titel,
                    }
                ),
                200,
            )

    # 3. Update Postgres
    update_postgres(model_uri=model_uri)
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    # (deprecated) DELETE current modelverzoek-triples in Fuseki toetsingslogboek
    # graph_uri = delete_modelverzoek_fuseki(modelverzoek_uri=modelverzoek_uri)

    # 4. DELETE AND INSERT new Modelverzoek-triples in Toetsingslogboek
    delete_and_insert_modelverzoek_into_toetsingslogboek(
        model_uri=model_uri, modelverzoek_uri=modelverzoek_uri
    )
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        # this was the old message as backup:
        # error_msg = f"Wijziging mislukt, gegevens kloppen (waarschijnlijk) niet.",
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )

    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "titel": titel,
                }
            ),
            200,
        )
    else:
        success_message = (
            f"{update} van modelverzoek '{upload_data['title_request']}' is gelukt.",
        )
        melding_manager.meld(ms.SUCCESS, success_message)
        return (
            jsonify(
                {
                    "resultaat": f"OK",
                    "titel": titel,
                }
            ),
            200,
        )


@app.route("/kennisgebiedenregister/read/<table>", methods=["GET"])
def lees_kennisgebiedenregister(table: str):
    """
    generic functie
    """
    global username
    primary_key_attributes = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]
    primary_key_values = [
        request.args.get(key) for key in request.args if key.startswith("primary_key_")
    ]

    if not primary_key_values:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "error": f"No primary_key found in query_params, make sure to go to /kennisgebiedenregister/update/{table}?primary_key_1=<your_primary_key_1>&primary_key_2=<your_primary_key_2",
                }
            ),
            400,
        )

    primary_key_query = " AND ".join(
        [
            f"{primary_key_attribute} = '{primary_key_value}'"
            for primary_key_attribute, primary_key_value in zip(
                primary_key_attributes, primary_key_values
            )
        ]
    )
    app.logger.info(
        [
            f"{primary_key_attribute} = '{primary_key_value}'"
            for primary_key_attribute, primary_key_value in zip(
                primary_key_attributes, primary_key_values
            )
        ]
    )

    query = f"SELECT * FROM {table} WHERE {primary_key_query}"
    app.logger.info(query)

    url_suffix_primary_keys = "&".join(
        [
            f"{primary_key_attribute}={primary_key_value}"
            for primary_key_attribute, primary_key_value in zip(
                primary_key_attributes, primary_key_values
            )
        ]
    )
    # default is read-only
    py_acties = {"read": routes["py_acties"][table]["read"] + url_suffix_primary_keys}
    gebruiker_acties = {
        key: value for key, value in routes["gebruiker_acties"][table].items()
    }

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        db_connection.commit()
        result = db_cursor.fetchone()

        app.logger.info(f"db result: {result}")
        app.logger.info(
            f"POSTGRES_MAPPING items: {cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items()}"
        )

        attribute_list = []
        for (attribute, _), value in zip(
            cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items(), result
        ):
            isTimeFormat = False
            if attribute == "laatste_wijziging":
                isTimeFormat = True

            attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][attribute]
            attribute_value_dict = {"attribuut": attribute}

            if attribute_metadata["type"] != "hide":
                for metadata in attribute_metadata:
                    if metadata == "waarde":
                        if isTimeFormat:
                            attribute_value_dict["waarde"] = value.strftime(
                                "%d-%m-%Y %H:%M"
                            )
                        else:
                            attribute_value_dict["waarde"] = (
                                value if value is not None else ""
                            )
                    else:
                        attribute_value_dict[metadata] = attribute_metadata[metadata]

                app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
                attribute_list.append(attribute_value_dict)

            if True:  # insert some condition if needed # todo
                # alles uitvoeren
                py_acties = {
                    key: value + url_suffix_primary_keys
                    for key, value in routes["py_acties"][table].items()
                }

            # else:
            #     # only read
            #     py_acties = {'read': routes[table]['read'] + primary_key_value}

        db_cursor.close()
        db_connection.close()
        return (
            jsonify(
                {
                    "resultaat": "OK",
                    "opmerking": "",
                    "py_acties": py_acties,
                    "gebruiker_acties": gebruiker_acties,
                    "titel": f"get from {table} the entry with: {url_suffix_primary_keys}",
                    "input_element": attribute_list,
                }
            ),
            200,
        )

    except Exception as e:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": f"{e}",
                    "py_acties": py_acties,
                    "gebruiker_acties": gebruiker_acties,
                    "titel": f"get from {table} the entry with: {url_suffix_primary_keys}",
                    "input_element": "[]",
                }
            ),
            500,
        )


@app.route("/kennisgebiedenregister/update/<table>", methods=["PUT"])
def update_kennisgebiedenregister(table: str):
    """
    params:
        data.get(id) is a uri
    """
    upload_data = dict(request.form)

    fields = []
    values = []
    primary_key_attributes = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]
    primary_key_values = [
        request.args.get(key) for key in request.args if key.startswith("primary_key_")
    ]
    if not primary_key_values:
        return (
            jsonify({"error": f"No primary_key_* found in query_params nor the url"}),
            400,
        )

    for key, _ in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items():
        if key in primary_key_attributes:
            continue
        value = upload_data.get(key)
        app.logger.info(f"{key}: {value}")

        if key != "laatste_wijziging" and value is not None:
            fields.append(f"{key} = %s")
            values.append(upload_data.get(key))

    # fields.append("laatste_wijziging = %s ")
    # values.append(datetime.now())

    if not fields:
        return jsonify({"error": "Geen velden om te wijzigen"}), 400

    primary_key_query = " AND ".join(
        [
            f"{primary_key_attribute} = %s"
            for primary_key_attribute in primary_key_attributes
        ]
    )

    query = f"""
        UPDATE {table}
        SET {', '.join(fields)}
        WHERE {primary_key_query}
    """
    app.logger.info(f"query = {query}")
    app.logger.info(f"values = {values}")

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query, tuple(values))
        # Check the number of updated rows
        rows_updated = db_cursor.rowcount
        db_connection.commit()
        db_cursor.close()
        db_connection.close()
        if rows_updated > 0:
            return (
                jsonify(
                    {
                        "resultaat": "OK",
                        "success": f"Update succesvol. {rows_updated} row(s) updated at {datetime.now()}",
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "resultaat": "ERROR",
                        "error": f"No rows updated. Are you sure this is the right user?",
                    }
                ),
                400,
            )
    except IndexError as e:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "error": f"IndexError: are you sure you have put all primary_keys in the url/query_params?",
                }
            ),
            400,
        )
    except Exception as e:
        return jsonify({"resultaat": "ERROR", "error": str(e)}), 500


@app.route("/toetsingsrapport/read", methods=["GET"])
@add_melding_manager
def lees_toetsingsrapport(melding_manager, table="toetsingsrapport"):
    primary_key_attribute = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS][0]
    primary_key_value = request.args.get(primary_key_attribute)
    opmerking = "Success"
    resultaat = "OK"

    if not primary_key_value:
        opmerking = f"No primary_key found in query_params"
        resultaat = "ERROR"
        melding_manager.meld(ms.ERROR, opmerking)

        return jsonify({"resultaat": resultaat, "opmerking": opmerking}), 200

    query = cn.POSTGRES_MAPPING[table][cn.READ_QUERY]
    values = [primary_key_value]
    result = ""
    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query, values)
        db_connection.commit()
        result_row = db_cursor.fetchone()
        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result: {result_row}")
        app.logger.info(
            f"POSTGRES_MAPPING items: {cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items()}"
        )
        app.logger.info(f"db columns: {column_names}")

        if result_row is not None:
            result = result_row[0]
        else:
            opmerking = "Toetsingsrapport niet gevonden"
            melding_manager.meld(ms.INFO, opmerking)

    except TypeError as e:
        if "'NoneType' object is not subscriptable" in str(e):
            opmerking = f"\nOphalen toetsingsrapport mislukt.\nDe inhoud van het toetsingsrapport '{primary_key_value}' kon niet worden opgehaald. Controleer of het toetsingsrapport nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hiervan een melding maken zodat het beheerteam u verder kan helpen."
            resultaat = "ERROR"
            melding_manager.meld(ms.ERROR, opmerking)
            app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")
        else:
            raise

    except Exception as e:
        resultaat = "ERROR"
        opmerking = f"\nOphalen toetsingsrapport mislukt.\nDe inhoud van het toetsingsrapport behorend bij modelverzoek '{primary_key_value}' kon niet worden opgehaald. Controleer of het modelverzoek nog bestaat. Mocht de fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
        melding_manager.meld(ms.ERROR, opmerking)
        app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")

    finally:
        global usergroep
        py_acties = {}
        attribute_list = []

        if usergroep in ["mau", "pow"]:
            py_acties = {
                name: route for name, route in routes["py_acties"][table].items()
            }
            for attribute, db_result_dict in cn.POSTGRES_MAPPING[table][
                cn.ATTRIBUTES
            ].items():
                if db_result_dict["type"] != "hide":
                    attribute_dict = db_result_dict
                    attribute_dict["attribuut"] = attribute
                    attribute_dict["waarde"] = result
                    attribute_list.append(attribute_dict)

    return (
        jsonify(
            {
                "resultaat": resultaat,
                "opmerking": opmerking,
                "py_acties": py_acties,
                "titel": f"Toetsingsrapport bij named_graph {primary_key_value}",
                "input_element": attribute_list,
            }
        ),
        200,
    )


@app.route("/toetsingsrapport/upload", methods=["POST"])
@add_melding_manager
def upload_toetsingsrapport(melding_manager, table="toetsingsrapport"):
    py_acties = {key: value for key, value in routes["py_acties"][table].items()}
    primary_key_attribute = cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS][0]
    upload_data = dict(request.form)
    titel = f""

    try:
        primary_key = upload_data[primary_key_attribute]
    except Exception as e:
        error_msg = (
            f"No primary_key found in query_params, make sure to add it to the formData"
        )
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": f"No primary_key found in query_params, make sure to add it to the formData",
                }
            ),
            200,
        )

    # upload_data = dict(request.form)
    # load binary_data to file_content_bytes
    # todo: should be an assertion instead of an if
    if (
        "binary_data" in request.files
        and request.files["binary_data"].filename != ""
        and request.files["binary_data"] != "undefined"
    ):

        # Retrieve the file from the request
        file = request.files["binary_data"]

        file_name = file.filename.replace(" ", "_")
        file_extension = "." + file_name.split(".")[-1:][0]
        file_title = file_name.replace(file_extension, "")
        file_content_bytes = file.read()
        required_file_extension_start = ".xl"

        # FOR PETER: WRITE REQUEST FILE_CONTENT_BYTES TO TEMPFILE
        dir_path = tempfile.mkdtemp()
        input_file_path = os.path.join(dir_path, file_name)
        app.logger.info(
            "FILE_CONTENT_BYTES UPLOAD TOETSINGSRAPPORT IS IN: " + str(input_file_path)
        )
        print(
            "FILE_CONTENT_BYTES UPLOAD TOETSINGSRAPPORT IS IN: " + str(input_file_path)
        )
        with open(input_file_path, "wb") as f:
            f.write(file_content_bytes)
        # END OF WRITING FILE_CONTENT_BYTES TO TEMPFILE

        if not file_extension.startswith(required_file_extension_start):
            error_msg = f"Het formaat van het aangeleverd bestand ({file_extension}) voldoet niet. Alleen een Excel conform het toetsingsrapport formaat ({required_file_extension_start}*) is toegestaan."
            melding_manager.meld(ms.ERROR, error_msg)

    else:
        error_msg = f"Geen bestand ontvangen"
        melding_manager.meld(ms.ERROR, error_msg)

    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        opmerking = error_melding[0].melding
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "opmerking": opmerking,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    # 1. Transform file into .ttl AND insert this ttl into Fuseki
    def transform_turtle_and_create_model_fuseki(
        input_file_path, model_type, trt_model_primary_key
    ):
        input_file_path = os.path.join(dir_path, file_name)
        print(input_file_path)
        with open(input_file_path, "wb") as f:
            f.write(file_content_bytes)

            # Transformation
            ## ALSO INSERTS THE NEW .ttl QUAD IN FUSEKI
            # Transformation in code
            try:
                turtle_creator = TurtleCreator(
                    [rf"{input_file_path}"],
                    model_type=model_type,
                    trt_model_primary_key=trt_model_primary_key,
                )
                turtle_creator.create_turtle()

            except ValueError as v:
                app.logger.exception(
                    f"ERROR: An exception occurred:\n{v}\n{traceback.format_stack()}"
                )
                melding = str(v)
                melding_manager.meld(ms.ERROR, melding)

            except Exception as e:
                app.logger.exception(
                    f"ERROR: An exception occurred:\n{e}\n{traceback.format_stack()}"
                )
                melding = f"Er is iets misgegaan bij de ttl-transformatie van het bestand. Weet je zeker dat je een BMS of Powerdesigner LDM bestand hebt gekozen?"
                melding_manager.meld(ms.ERROR, melding)

        return str(turtle_creator.uri), turtle_creator.get_vda_model_name()

    # 2. get old_toetsingsrapport_uri from PostgreSQL database
    def get_old_toetsingsrapport_uri():
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        old_toetsingsrapport_uri = ""
        with db_cursor:
            query = f"""
            SELECT toetsingsrapport_uri from toetsingsrapport WHERE named_graph = %s
                 """
            values = [primary_key]

            #        AND bd_user = '{username}'
            app.logger.info(f"query = {query}")
            app.logger.info(f"values = {[value[:100] for value in values]}")

            try:
                # First retrieve old_toetsingsrapport_uri before uploading the new one
                db_connection = psycopg2.connect(**config.DB_CONFIG)
                db_cursor = db_connection.cursor()
                db_cursor.execute(query, values)
                # Check the number of updated rows

                old_toetsingsrapport_uri_row = db_cursor.fetchone()
                if old_toetsingsrapport_uri_row:
                    old_toetsingsrapport_uri = old_toetsingsrapport_uri_row[0]
                else:
                    old_toetsingsrapport_uri = None

                ic(old_toetsingsrapport_uri)

                db_connection.commit()
                db_cursor.close()
                db_connection.close()

            except Exception as e:
                app.logger.exception(f"{e}: {traceback.format_exc()}")
                error_msg = f"\nUploaden toetsingsrapport mislukt.\nDe inhoud van het toetsingsrapport '{file_name}' kon niet worden opgehaald. Controleer of de inhoud van het bestand klopt. Mocht de fout zich blijven herhalen, dan kunt u hiervan een melding maken zodat het beheerteam u verder kan helpen."
                melding_manager.meld(ms.ERROR, error_msg)

        return old_toetsingsrapport_uri

    # 3. put in PostgreSQL database
    def put_in_postgreSQL():
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        with db_cursor:
            query = f"""
                   {cn.POSTGRES_MAPPING[table][cn.INSERT_QUERY]}
                 """
            values = [
                primary_key,
                file_content_bytes,
                file_name,
                new_toetsingsrapport_uri,
            ]

            #        AND bd_user = '{username}'
            app.logger.info(f"query = {query}")
            app.logger.info(f"values = {[value[:100] for value in values]}")

            try:
                db_connection = psycopg2.connect(**config.DB_CONFIG)
                db_cursor = db_connection.cursor()
                db_cursor.execute(query, values)
                # Check the number of updated rows
                rows_updated = db_cursor.rowcount
                ic(rows_updated)
                try:
                    ic(db_cursor.fetchone())
                except psycopg2.ProgrammingError:
                    pass
                db_connection.commit()
                db_cursor.close()
                db_connection.close()

            except Exception as e:
                app.logger.exception(f"{e}: {traceback.format_exc()}")
                error_msg = f"\nUploaden toetsingsrapport mislukt.\nDe inhoud van het toetsingsrapport '{file_name}' kon niet worden opgehaald. Controleer of de inhoud van het bestand klopt. Mocht de fout zich blijven herhalen, dan kunt u hiervan een melding maken zodat het beheerteam u verder kan helpen."
                melding_manager.meld(ms.ERROR, error_msg)
                return (
                    jsonify(
                        {
                            "resultaat": "ERROR",
                            "opmerking": error_msg,
                            "py_acties": py_acties,
                            "titel": f"Upload toetsingsrapport",
                        }
                    ),
                    200,
                )

    # 4. Delete old model in case it exists (doesnt return error if not existing)
    def delete_model_fuseki(named_graph_to_delete: str):
        try:
            url = config.DATASTORE_ENDPOINT + "/update"

            payload = f"DELETE WHERE {{\n  GRAPH <{named_graph_to_delete}> {{\n    ?s ?p ?o .\n  }}\n}} ;"
            headers = {
                "Content-Type": "application/sparql-update",
            }

            response = requests.request(
                "POST", url, data=payload, headers=headers, verify=False
            )

            if response.status_code != 204:
                # Log every possible detail of the response
                app.logger.info("------ RESPONSE DETAILS ------")

                # 1. Status Code
                app.logger.info(f"Status Code: {response.status_code}")

                # 2. Response Text (Raw text of the response)
                app.logger.info(f"Response Text: {response.text}")

                # 3. Try to log JSON response (if applicable)
                try:
                    json_data = response.json()
                    app.logger.info(f"Response JSON: {json_data}")
                except ValueError:
                    app.logger.info("Response is not in JSON format.")

                # 4. Reason (Textual description of the HTTP response status code)
                app.logger.info(f"Reason: {response.reason}")

                # 5. Request Information (Request object containing details about the request)
                app.logger.info(f"Request URL: {response.request.url}")
                app.logger.info(f"Request Method: {response.request.method}")
                app.logger.info(f"Request Headers: {response.request.headers}")
                app.logger.info(f"Request Body: {response.request.body}")

                # 6. Response Headers (All the headers received in the response)
                app.logger.info(f"Response Headers: {response.headers}")

                # 7. Elapsed Time (Time taken to complete the request)
                app.logger.info(f"Elapsed Time: {response.elapsed}")

                # 8. History (In case of redirects, history will have the list of requests made)
                app.logger.info(f"Response History: {response.history}")

                # 9. Cookies (Any cookies received in the response)
                app.logger.info(f"Response Cookies: {response.cookies}")

                # 10. Encoding (The apparent encoding used by the server for the response)
                app.logger.info(f"Response Encoding: {response.encoding}")

                # 11. Raw Response Content (Binary content, useful for non-text responses)
                app.logger.info(
                    f"Raw Response Content (first 100 bytes): {response.content[:100]}"
                )

                # 12. Check if the response is a redirect
                app.logger.info(f"Is Redirect: {response.is_redirect}")
                error_message = (
                    f"FusekiError: Fuseki failed to delete model graph"
                    + f"\nEr is iets misgegaan bij de ttl-transformatie van het bestand. Weet je zeker dat je een BMS of Powerdesigner LDM bestand hebt gekozen?"
                )
                melding_manager.meld(ms.ERROR, error_message)
            else:
                app.logger.info(
                    f"Succesfully deleted <{named_graph_to_delete}> in fuseki, returned: {response}"
                )
                # success_message = f"Succesfully deleted <{graph_uri}> in fuseki, returned: {response}"
                # melding_manager.meld(ms.SUCCESS, success_message)

        except Exception as e:
            error_message = f"{e}: {traceback.format_exc()}"
            melding_manager.meld(ms.ERROR, error_message)

    # 1. Transform file into .ttl AND insert this ttl into Fuseki
    (new_toetsingsrapport_uri, vda_model_name) = (
        transform_turtle_and_create_model_fuseki(
            input_file_path, "TRT", trt_model_primary_key=primary_key
        )
    )
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        opmerking = error_melding[0].melding
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "opmerking": opmerking,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    # 2. get old_toetsingsrapport_uri from PostgreSQL database
    try:
        old_toetsingsrapport_uri = get_old_toetsingsrapport_uri()
    except Exception as e:
        error_msg = f"\nUploaden toetsingsrapport mislukt.\nDe inhoud van het oude toetsingsrapport kon niet worden opgehaald. Mocht de fout zich blijven herhalen, dan kunt u hiervan een melding maken zodat het beheerteam u verder kan helpen."
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": ms.ERROR,
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    # 3. put in PostgreSQL database
    put_in_postgreSQL()
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        opmerking = error_melding[0].melding
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "opmerking": opmerking,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    # 4. Delete old model in case it exists (doesnt return error if not existing)
    if (old_toetsingsrapport_uri != new_toetsingsrapport_uri) and (
        old_toetsingsrapport_uri is not None
    ):
        delete_model_fuseki(named_graph_to_delete=old_toetsingsrapport_uri)
    error_melding = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_melding:
        resultaat = ms.ERROR
        opmerking = error_melding[0].melding
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "opmerking": opmerking,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    app.logger.info(melding_manager)

    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": f"Gegevens succesvol opgeslagen voor model '{file_title}' met uri: '{new_toetsingsrapport_uri}'.",
                "py_acties": py_acties,
                "titel": titel,
            }
        ),
        200,
    )


@app.route("/docs/toetsingskader", methods=["GET"])
def index_docs():
    directory = os.path.join("static", "docs")
    app.logger.info(os.path.isfile(directory + os.sep + "index.html"))
    return send_from_directory(directory, "index.html")


@app.route("/rapportage_query/all", methods=["GET"])
@add_melding_manager
def get_rapportage_query_all(melding_manager):
    app.logger.info("in queries func")
    table = "rapportage_query"
    query = f"""SELECT rapportage_query_id, naam, beschrijving, modeltype FROM {table} ORDER BY naam ASC"""

    app.logger.info(f"query = {query}")
    py_acties = {}
    gebruiker_acties = {
        "+ Query toevoegen": routes["gebruiker_acties"][table]["+ Query toevoegen"]
    }
    app.logger.info(
        "visits_unaccessible_page usr: "
        + usergroep
        + " path: "
        + routes["gebruiker_acties"][table]["+ Query toevoegen"]
        + " result: "
        + str(
            visits_unaccessible_page(
                role=usergroep,
                path=routes["gebruiker_acties"][table]["+ Query toevoegen"],
            )
        )
    )

    if visits_unaccessible_page(
        role=usergroep, path=routes["gebruiker_acties"][table]["+ Query toevoegen"]
    ):
        del gebruiker_acties["+ Query toevoegen"]

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        # Check the number of updated rows
        rows_retrieved = db_cursor.rowcount
        db_connection.commit()
        result = db_cursor.fetchall()

        app.logger.info("rows retrieved: " + str(rows_retrieved))

        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result column names: {column_names}")
        app.logger.info(f"db result column values: {result}")

        db_cursor.close()
        db_connection.close()
        app.logger.info(
            f"got original document from postgres"
        )  # remove binary_data for log?
        # return dict(zip(column_names, result))

    except Exception as e:
        error_msg = f"\nOphalen lijst van rapportage-queries mislukt.\nControleer of deze lijst bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."

        app.logger.exception(f"{error_msg}{''.join(traceback.format_stack())}")
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": f"Upload toetsingsrapport",
                }
            ),
            200,
        )

    rapportage_query_dict = {}
    for i, rapportage_query in enumerate(result):
        list_of_attribute_dicts = []
        attribute_value_dict = dict(zip(column_names, rapportage_query))
        for column_name, attribute_value in attribute_value_dict.items():
            if column_name in ("naam", "beschrijving", "modeltype"):
                attribute_dict = {"attribuut": column_name, "waarde": attribute_value}
                if column_name == "naam":
                    attribute_dict["link"] = attribute_value_dict["rapportage_query_id"]
                list_of_attribute_dicts.append(attribute_dict)

        rapportage_query_dict[i] = list_of_attribute_dicts

    melding_manager.meld()
    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "Gegevens succesvol opgehaald.",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "titel": f"Overzicht Queries",
                cn.KOLOMMEN: cn.POSTGRES_MAPPING[table][cn.KOLOMMEN],
                "input_regels": rapportage_query_dict,
            }
        ),
        200,
    )


@app.route("/rapportage_query/<modeltype>/all", methods=["GET"])
@add_melding_manager
def get_rapportage_query_all_by_modeltype(melding_manager, modeltype):
    app.logger.info("in queries func")
    table = "rapportage_query"
    query = f"""SELECT rapportage_query_id, naam, beschrijving, modeltype, sparql_query FROM {table}
                WHERE modeltype = '{modeltype}'; """

    app.logger.info(f"query = {query}")
    py_acties = {}
    gebruiker_acties = {}

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        # Check the number of updated rows
        rows_retrieved = db_cursor.rowcount
        db_connection.commit()
        result = db_cursor.fetchall()

        app.logger.info("rows retrieved: " + str(rows_retrieved))

        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result column names: {column_names}")
        app.logger.info(f"db result column values: {result}")

        db_cursor.close()
        db_connection.close()
        app.logger.info(
            f"got original document from postgres"
        )  # remove binary_data for log?
        # return dict(zip(column_names, result))

    except Exception as e:
        error_msg = f"\nOphalen lijst van rapportage-queries mislukt.\nControleer of deze lijst bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."

        app.logger.exception(f"{error_msg}{''.join(traceback.format_stack())}")
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": f"Ophalen Rapportage Query",
                }
            ),
            200,
        )

    rapportage_query_dict = {}
    for i, rapportage_query in enumerate(result):
        list_of_attribute_dicts = []
        attribute_value_dict = dict(zip(column_names, rapportage_query))
        for column_name, attribute_value in attribute_value_dict.items():
            if column_name in ("naam", "beschrijving", "modeltype"):
                attribute_dict = {"attribuut": column_name, "waarde": attribute_value}
                if column_name == "naam":
                    attribute_dict["link"] = attribute_value_dict["rapportage_query_id"]
                list_of_attribute_dicts.append(attribute_dict)

        rapportage_query_dict[i] = list_of_attribute_dicts

    melding_manager.meld()
    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "Gegevens succesvol opgehaald.",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "titel": f"Overzicht Queries",
                cn.KOLOMMEN: cn.POSTGRES_MAPPING[table][cn.KOLOMMEN],
                "input_regels": rapportage_query_dict,
            }
        ),
        200,
    )


@app.route("/rapportage_query/read", methods=["GET"])
@add_melding_manager
def read_rapportage_query(melding_manager):
    table = "rapportage_query"
    if request.args:
        primary_key = [request.args.get(keyname) for keyname in request.args][0].strip()
        keyname = [keyname for keyname in request.args][0]

    if not keyname:
        error_msg = f"No primary_key found in query_params, make sure to go to /rapportage_query/read?pk=<your_pk>"
        melding_manager.meld(ms.ERROR, error_msg)

        return jsonify({"resultaat": "ERROR"})

    query = f"""SELECT * FROM {table} WHERE rapportage_query_id='{primary_key}'"""

    app.logger.info(f"query = {query}")
    py_acties = {}
    gebruiker_acties = {
        "Wijzigen": routes["gebruiker_acties"][table]["wijzigen_pagina"]
        + f"?rapportage_query_id={primary_key}",
        "Verwijderen": routes["gebruiker_acties"][table]["delete"]
        + f"?rapportage_query_id={primary_key}",
    }

    if visits_unaccessible_page(
        role=usergroep, path=routes["gebruiker_acties"][table]["wijzigen_pagina"]
    ):
        del gebruiker_acties["Wijzigen"]

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        # Check the number of updated rows
        rows_retrieved = db_cursor.rowcount
        db_connection.commit()
        result = db_cursor.fetchone()

        app.logger.info("rows retrieved: " + str(rows_retrieved))

        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result column names: {column_names}")
        app.logger.info(f"db result column values: {result}")

        db_cursor.close()
        db_connection.close()
        app.logger.info(
            f"got original document from postgres"
        )  # remove binary_data for log?
        # return dict(zip(column_names, result))

    except Exception as e:
        error_msg = f"\nOphalen van rapportage-query met id {primary_key} mislukt.\nControleer of deze rapportage-query bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."

        app.logger.exception(f"{error_msg}{''.join(traceback.format_stack())}")
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": f"Ophalen Rapportage Query",
                }
            ),
            200,
        )

    db_result_dict = dict(zip(column_names, result))

    attribute_list = []
    for colname in db_result_dict:
        if colname in cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]:
            continue
        value = db_result_dict[colname]

        attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][colname]
        attribute_value_dict = {"attribuut": colname}
        if attribute_metadata["type"] != "hide":
            for metadata in attribute_metadata:
                match metadata:
                    case "waarde":
                        attribute_value_dict[metadata] = (
                            value if not None else "leeg"
                        )  # leeg could be changed for empty string. 'leeg' is chosen to distinguish between nothing being sent, or empty being sent
                    case "type":
                        if colname != "sparql_query":
                            attribute_value_dict[metadata] = "display"
                        else:
                            attribute_value_dict[metadata] = attribute_metadata[
                                metadata
                            ]
                    case "verplicht":
                        attribute_value_dict[metadata] = "readonly"
                    case _:
                        attribute_value_dict[metadata] = attribute_metadata[metadata]

        app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
        attribute_list.append(attribute_value_dict)

    sorted_attribute_list = sorted(attribute_list, key=lambda x: x["position"])
    app.logger.info(sorted_attribute_list)

    melding_manager.meld()
    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "titel": f"Bestaande query",
                cn.KOLOMMEN: cn.POSTGRES_MAPPING[table][cn.KOLOMMEN],
                "input_element": sorted_attribute_list,
            }
        ),
        200,
    )


@app.route(
    "/rapportage_query/wijzigen_pagina", methods=["POST"]
)  # TODO should be GET, after refactor
@add_melding_manager
def read_rapportage_query_wijzigen_pagina(melding_manager):
    table = "rapportage_query"

    try:
        primary_key = [request.args.get(key) for key in request.args][0]
    except Exception as e:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "error": f"No primary_key found in query_params, make sure to go to /rapportage_query/wijzigen_pagina?pk=<your_pk>",
                }
            ),
            400,
        )

    query = f"""SELECT * FROM {table} WHERE rapportage_query_id='{primary_key}'"""

    app.logger.info(f"query = {query}")
    py_acties = {
        "Wijzigingen opslaan": routes["py_acties"][table]["Wijzigingen opslaan"]
        + f"?rapportage_query_id={primary_key}"
    }
    gebruiker_acties = {}

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        # Check the number of updated rows
        rows_retrieved = db_cursor.rowcount
        db_connection.commit()
        result = db_cursor.fetchone()

        app.logger.info("rows retrieved: " + str(rows_retrieved))

        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result column names: {column_names}")
        app.logger.info(f"db result column values: {result}")

        db_cursor.close()
        db_connection.close()
        app.logger.info(
            f"got original document from postgres"
        )  # remove binary_data for log?
        # return dict(zip(column_names, result))

    except Exception as e:
        error_msg = f"\nOphalen van rapportage-query met id {primary_key} mislukt.\nControleer of deze rapportage-query bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."

        app.logger.exception(f"{error_msg}{''.join(traceback.format_stack())}")
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": f"Ophalen Wijzigpagina Rapportage Query",
                }
            ),
            200,
        )

    db_result_dict = dict(zip(column_names, result))

    attribute_list = []
    for colname in db_result_dict:
        if colname in cn.POSTGRES_MAPPING[table][cn.PRIMARY_KEYS]:
            continue
        value = db_result_dict[colname]

        attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][colname]
        attribute_value_dict = {"attribuut": colname}
        if attribute_metadata["type"] != "hide":
            for metadata in attribute_metadata:
                match metadata:
                    case "waarde":
                        attribute_value_dict["waarde"] = (
                            value if not None else "leeg"
                        )  # leeg could be changed for empty string. 'leeg' is chosen to distinguish between nothing being sent, or and empty being sent
                    case _:
                        attribute_value_dict[metadata] = attribute_metadata[metadata]
        app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
        attribute_list.append(attribute_value_dict)

    def get_models_fuseki():
        url = config.DATASTORE_ENDPOINT + "/query"

        payload = {"query": cn.GET_FUSEKI_MODELS_SPARQL_QUERY}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        response = requests.request(
            "POST", url, data=payload, headers=headers, verify=False
        )
        if response.status_code == 200:
            # Parse XML manually
            app.logger.info(f"json: {response.json()}")

            # Extract the URIs
            modelversies = [
                binding["modelversie"]["value"]
                for binding in response.json()["results"]["bindings"]
            ]
            modelversie_namen = [
                binding["modelversie_label"]["value"]
                for binding in response.json()["results"]["bindings"]
            ]

            app.logger.info(f"modelversies: {modelversies}")
            app.logger.info(
                f'modelversie_label: {[binding["modelversie_label"]["value"] for binding in response.json()["results"]["bindings"]]}'
            )
            models_dict = dict(zip(modelversies, modelversie_namen))
            models_dict[""] = ""
            return models_dict
        else:
            warn_msg = (
                f"Failed to query Fuseki. HTTP Status Code: {response.status_code}"
            )
            app.logger.info(warn_msg)
            app.logger.info(f"Response: {response.text}")
            melding_manager.meld(meldattr(ms.WARNING, warn_msg, attribuut="model"))
            return []

    model_dict = {"attribuut": "model"} | cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][
        "model"
    ]
    model_dict["opties"] = get_models_fuseki()
    attribute_list.append(model_dict)

    test_button_dict = {
        "attribuut": "query_testen",
        "label": "Query testen",
        "position": 3,
        "type": "button",
        "waarde": "",
        "link": routes["gebruiker_acties"][table]["run_query"],
    }
    attribute_list.append(test_button_dict)

    sorted_attribute_list = sorted(attribute_list, key=lambda x: x["position"])
    app.logger.info(sorted_attribute_list)
    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "titel": f"Bestaande query",
                cn.KOLOMMEN: cn.POSTGRES_MAPPING[table][cn.KOLOMMEN],
                "input_element": sorted_attribute_list,
            }
        ),
        200,
    )


@app.route(
    "/rapportage_query/update", methods=["POST"]
)  # TODO should be PUT, after refactor
@add_melding_manager
def update_rapportage_query(melding_manager):
    table = "rapportage_query"

    primary_key = [request.args.get(key) for key in request.args][0]
    if not primary_key:
        return (
            jsonify(
                {
                    "error": f"No primary_key found in query_params, make sure to go to /rapportage_query/update?pk=<your_pk>"
                }
            ),
            400,
        )

    query = f"""
            {cn.POSTGRES_MAPPING[table][cn.UPDATE_QUERY]}
          """
    upload_data = dict(request.form)

    values = []
    values.append(upload_data["sparql_query"])
    values.append(upload_data["naam"])
    values.append(upload_data["beschrijving"])
    values.append(upload_data["modeltype"])

    values.append(primary_key)
    app.logger.info(f"values = {[value[:100] for value in values]}")
    app.logger.info(f"query = {query}")
    py_acties = {
        "Wijzigingen opslaan": routes["py_acties"][table]["Wijzigingen opslaan"]
    }
    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query, values)
        # Check the number of updated rows
        rows_affected = db_cursor.rowcount
        db_connection.commit()

        app.logger.info("rows affected: " + str(rows_affected))
        db_cursor.close()
        db_connection.close()

    except Exception as e:
        # raise e
        error_msg = f"{e}: {traceback.format_exc()}"
        app.logger.exception(
            f"ERROR: An exception occurred:\n{error_msg}{''.join(traceback.format_stack())}"
        )
        melding_manager.meld(ms.WARNING, "Er is iets misgegaan in de database")

        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "gebruiker_acties": {},
                }
            ),
            200,
        )
    melding_manager.meld()

    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "",
                "py_acties": py_acties,
                "gebruiker_acties": {},
                "success": "rows affected: " + str(rows_affected),
            }
        ),
        200,
    )


@app.route(
    "/rapportage_query/create_pagina", methods=["POST"]
)  # TODO should be GET, after refactor
@add_melding_manager
def read_rapportage_query_create_pagina(melding_manager):
    table = "rapportage_query"
    py_acties = {"Query opslaan": routes["py_acties"][table]["Query opslaan"]}
    gebruiker_acties = {}
    # todo: in de refactor bepalen wanneer iets py_actie of gebruiker_actie is

    attribute_list = []
    for colname in cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES]:
        if colname == "model":
            continue
        attribute_metadata = cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][colname]
        attribute_value_dict = {"attribuut": colname}
        if attribute_metadata["type"] != "hide":
            for metadata in attribute_metadata:
                match metadata:
                    case _:
                        attribute_value_dict[metadata] = attribute_metadata[metadata]

            app.logger.info(f"attribute_value_dict : {attribute_value_dict}")
            attribute_list.append(attribute_value_dict)

    def get_models_fuseki():
        url = config.DATASTORE_ENDPOINT + "/query"

        payload = {"query": cn.GET_FUSEKI_MODELS_SPARQL_QUERY}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        response = requests.request(
            "POST", url, data=payload, headers=headers, verify=False
        )
        if response.status_code == 200:
            # Parse XML manually
            app.logger.info(f"json: {response.json()}")

            # Extract the URIs
            modelversies = [
                binding["modelversie"]["value"]
                for binding in response.json()["results"]["bindings"]
            ]
            modelversie_namen = [
                binding["modelversie_label"]["value"]
                for binding in response.json()["results"]["bindings"]
            ]

            app.logger.info(f"modelversies: {modelversies}")
            app.logger.info(
                f'modelversie_label: {[binding["modelversie_label"]["value"] for binding in response.json()["results"]["bindings"]]}'
            )
            models_dict = dict(zip(modelversies, modelversie_namen))
            models_dict[""] = ""
            return models_dict
        else:
            print(f"Failed to query Fuseki. HTTP Status Code: {response.status_code}")
            print(f"Response: {response.text}")
            return []

    model_dict = {"attribuut": "model"} | cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES][
        "model"
    ]
    model_dict["opties"] = get_models_fuseki()
    attribute_list.append(model_dict)

    test_button_dict = {
        "attribuut": "query_testen",
        "label": "Query testen",
        "position": 3,
        "type": "button",
        "waarde": "",
        "link": routes["gebruiker_acties"][table]["run_query"],
    }
    attribute_list.append(test_button_dict)

    sorted_attribute_list = sorted(attribute_list, key=lambda x: x["position"])
    app.logger.info(sorted_attribute_list)

    return (
        jsonify(
            {
                "resultaat": "OK",
                "opmerking": "",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "titel": f"Bestaande query",
                cn.KOLOMMEN: cn.POSTGRES_MAPPING[table][cn.KOLOMMEN],
                "input_element": sorted_attribute_list,
            }
        ),
        200,
    )


@app.route("/rapportage_query/create", methods=["POST"])
@add_melding_manager
def create_rapportage_query(melding_manager):
    table = "rapportage_query"

    query = f"""
            {cn.POSTGRES_MAPPING[table][cn.INSERT_QUERY]}
          """
    if not request.form:
        return (
            jsonify(
                {
                    "error": f"No primary_key found in query_params, make sure to send to /rapportage_query/create with formData"
                }
            ),
            400,
        )
    upload_data = dict(request.form)

    values = []
    values.append(upload_data["sparql_query"])
    values.append(upload_data["naam"])
    values.append(upload_data["beschrijving"])
    values.append(upload_data["modeltype"])

    app.logger.info(f"upload_data = {upload_data}")

    app.logger.info(f"values = {[value[:100] for value in values]}")
    app.logger.info(f"query = {query}")

    py_acties = {key: value for key, value in routes["py_acties"][table].items()}
    gebruiker_acties = {
        "wijzigen": routes["gebruiker_acties"][table]["wijzigen_pagina"]
    }

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query, values)
        # Check the number of updated rows
        rows_affected = db_cursor.rowcount
        db_connection.commit()

        app.logger.info("rows affected: " + str(rows_affected))
        db_cursor.close()
        db_connection.close()

    except Exception as e:
        # raise e
        error_msg = "Er is iets misgegaan in de database. Controleer of de rapportage_query nog bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
        app.logger.exception(f" {error_msg} {''.join(traceback.format_stack())}")
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "py_acties": py_acties,
                    "gebruiker_acties": {},
                }
            ),
            200,
        )

    melding_manager.meld(
        ms.SUCCESS, f"Rapportage-query '{upload_data['naam']}' is opgeslagen"
    )
    return (
        jsonify(
            {
                "resultaat": "OK",
                "py_acties": py_acties,
                "gebruiker_acties": gebruiker_acties,
                "success": "rows affected: " + str(rows_affected),
            }
        ),
        200,
    )


@app.post("/rapportage_query/delete")
@add_melding_manager
def delete_rapportage_query(melding_manager):
    table = "rapportage_query"

    primary_key = [request.args.get(key) for key in request.args][0]
    if not primary_key:
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "error": f"No primary_key found in query_params, make sure to go to /rapportage_query/update/?rapportage_query_id=<rapportage_query_id>",
                }
            ),
            400,
        )

    query = f"""
            {cn.POSTGRES_MAPPING[table][cn.DELETE_QUERY]}
          """

    values = []
    values.append(primary_key)
    app.logger.info(f"query = {values}")
    app.logger.info(f"query = {query}")
    py_acties = {}

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query, values)
        # Check the number of updated rows
        rows_affected = db_cursor.rowcount
        db_connection.commit()

        app.logger.info("rows affected: " + str(rows_affected))
        db_cursor.close()
        db_connection.close()

    except Exception as e:
        # raise e
        error_msg = "Er is iets misgegaan in de database. Controleer of de rapportage_query nog bestaat. Mocht deze fout zich blijven herhalen, dan kunt u hier een melding van maken zodat het beheerteam u verder kan helpen."
        app.logger.exception(f" {error_msg} {''.join(traceback.format_stack())}")
        melding_manager.meld(ms.WARNING, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "gebruiker_acties": {},
                }
            ),
            200,
        )
    success_msg = f"Uw rapportage-query is succesvol verwijderd! rapportage-query: '{primary_key}'"
    melding_manager.meld(ms.SUCCESS, success_msg)
    app.logger.info(success_msg)

    return (
        jsonify(
            {
                "resultaat": "OK",
                "py_acties": py_acties,
                "gebruiker_acties": {},
                "success": "rows affected: " + str(rows_affected),
            }
        ),
        200,
    )


@app.route("/kennisgebiedenregister_bestand/read", methods=["GET"])
@add_melding_manager
def read_kennisgebiedenregister_bestand_pagina(
    melding_manager, table="kennisgebiedenregister_bestand"
):
    query = cn.POSTGRES_MAPPING[table][cn.READ_QUERY]
    result = ""
    resultaat_bericht = "OK"

    try:
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        db_cursor.execute(query)
        db_connection.commit()
        result_row = db_cursor.fetchone()
        column_names = [desc[0] for desc in db_cursor.description]

        app.logger.info(f"db result: {result_row}")
        app.logger.info(
            f"POSTGRES_MAPPING items: {cn.POSTGRES_MAPPING[table][cn.ATTRIBUTES].items()}"
        )
        app.logger.info(f"db columns: {column_names}")

        if result_row is not None:
            result = result_row[0]
        else:
            opmerking = "Er is geen kennisgebiedenregister-bestand gevonden"
            melding_manager.meld(ms.INFO, opmerking)

    except TypeError as e:
        if "'NoneType' object is not subscriptable" in str(e):
            opmerking = f"\nOphalen kennisgebiedenregister-bestand mislukt.\nDe inhoud van het kennisgebiedenregister-bestand kon niet worden opgehaald."
            resultaat_bericht = "ERROR"
            melding_manager.meld(ms.ERROR, opmerking)
            app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")
        else:
            raise

    except Exception as e:
        resultaat_bericht = "ERROR"
        opmerking = f"\nOphalen kennisgebiedenregister-bestand.\nDe inhoud van het kennisgebiedenregister-bestand kon niet worden opgehaald."
        melding_manager.meld(ms.ERROR, opmerking)
        app.logger.exception(f"{e}: {''.join(traceback.format_stack())}")

    finally:
        global usergroep
        py_acties = {}
        attribute_list = []

        if usergroep in ["mau", "pow"]:
            py_acties = {
                name: route for name, route in routes["py_acties"][table].items()
            }
            for attribute, db_result_dict in cn.POSTGRES_MAPPING[table][
                cn.ATTRIBUTES
            ].items():
                if db_result_dict["type"] != "hide":
                    attribute_dict = db_result_dict
                    attribute_dict["attribuut"] = attribute
                    attribute_dict["waarde"] = result
                    attribute_list.append(attribute_dict)

        return (
            jsonify(
                {
                    "resultaat": resultaat_bericht,
                    "py_acties": py_acties,
                    "titel": f"Kennisgebiedenregister uploaden",
                    "input_element": attribute_list,
                }
            ),
            200,
        )


@app.route("/kennisgebiedenregister_bestand/upload", methods=["POST"])
@add_melding_manager
def upload_kennisgebiedenregister_bestand(
    melding_manager, table="kennisgebiedenregister_bestand"
):
    py_acties = {}
    titel = f"Upload kennisgebiedenregister"
    kennisgebiedenregister_uri = "urn:name:kennisgebiedenregister"
    resultaat = "OK"

    # upload_data = dict(request.form)
    # load binary_data to file_content_bytes
    # todo: perhaps should be an assertion instead of an if
    if (
        "binary_data" in request.files
        and request.files["binary_data"].filename != ""
        and request.files["binary_data"] != "undefined"
    ):

        # Retrieve the file from the request
        file = request.files["binary_data"]

        file_name = file.filename.replace(" ", "_")
        file_extension = "." + file_name.split(".")[-1:][0]
        file_title = file_name.replace(file_extension, "")
        file_content_bytes = file.read()
        required_file_extension_start = ".ttl"

        if not file_extension.startswith(required_file_extension_start):
            error_msg = f"Het formaat van het aangeleverd bestand ({file_extension}) voldoet niet. Alleen een shacl bestand ({required_file_extension_start}) is toegestaan."
            melding_manager.meld(ms.ERROR, error_msg)

    else:
        error_msg = f"Geen bestand ontvangen"
        melding_manager.meld(ms.ERROR, error_msg)

    error_meldingen = [
        melding for melding in melding_manager if melding.soort == ms.ERROR
    ]
    if error_meldingen:
        resultaat = ms.ERROR
        opmerking = error_meldingen[0].melding
        return (
            jsonify(
                {
                    "resultaat": resultaat,
                    "opmerking": opmerking,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )

    # 1.
    def validate_syntactically_using_rdflib(file_content_bytes: bytes):
        try:
            g = Graph()
            g.parse(
                file_content_bytes,
                format="turtle",
            )
        except BadSyntax as bs:
            app.logger.info(f"Bad Syntax!")
            line = bs.args[1]
            all_text = bs.args[2]
            why = bs._why
            relevant_text = all_text.split("\n")[line]

            error_msg = (
                f"Bad Syntax found at line {line + 1}, {why}: \n {relevant_text}"
            )
            raise Exception(error_msg)
        except Exception as e:
            app.logger.exception(f"{e}: {traceback.format_exc()}")
            app.logger.info(f"Bad Syntax! - Couldnt check")
            error_msg = f"\nUploaden kennisgebiedenregister mislukt.\nDe inhoud van het bestand '{file_name}' kon niet worden gecheckt op Syntax. Controleer of de inhoud van het bestand klopt. Mocht dit probleem zich blijven voordoen neem dan contact met het beheerteam via Modellenbibliotheek@belastingdienst.nl."
            melding_manager.meld(ms.ERROR, error_msg)

        return g

    # 2.
    def validate_semantically_using_pyshacl(g: Graph):
        shacl_graph = Graph().parse(
            cn.SHACL_KENNISGEBIEDENREGISTER_TTL,
            format="turtle",
        )

        r = pyshacl.validate(
            g,
            shacl_graph=shacl_graph,
            inference="rdfs",
            abort_on_first=False,
            allow_infos=False,
            allow_warnings=False,
            meta_shacl=False,
            advanced=False,
            js=False,
            debug=False,
        )
        conforms, results_graph, results_text = r
        app.logger.info(f"Conforms: {conforms}")
        if not conforms:
            error_msg = f"Bad Semantics found\n{results_text}"
            melding_manager.meld(ms.ERROR, error_msg)
        else:
            return g

    # 3. put in PostgreSQL database
    def put_in_postgreSQL(table, values):
        db_connection = psycopg2.connect(**config.DB_CONFIG)
        db_cursor = db_connection.cursor()
        with db_cursor:
            query = f"""
                       {cn.POSTGRES_MAPPING[table][cn.INSERT_QUERY]}
                     """

            #        AND bd_user = '{username}'
            app.logger.info(f"query = {query}")
            app.logger.info(f"values = {[value[:100] for value in values]}")

            try:
                db_connection = psycopg2.connect(**config.DB_CONFIG)
                db_cursor = db_connection.cursor()
                db_cursor.execute(query, values)
                # Check the number of updated rows
                rows_updated = db_cursor.rowcount
                ic(rows_updated)
                try:
                    ic(db_cursor.fetchone())
                except psycopg2.ProgrammingError:
                    pass
                db_connection.commit()
                db_cursor.close()
                db_connection.close()

            except Exception as e:
                app.logger.exception(f"{e}: {traceback.format_exc()}")
                raise Exception(e)

    # 5. Add new graph
    def add_graph_fuseki_from_bytes(named_graph_to_add: str, file_content_bytes):
        try:
            url = config.DATASTORE_ENDPOINT + "?graph=" + named_graph_to_add
            headers = {"Content-Type": "text/turtle;charset=utf-8"}
            response = requests.put(
                url, data=file_content_bytes, headers=headers, verify=False
            )

            if (response.status_code != 204) & (response.status_code != 201):
                # Log every possible detail of the response
                app.logger.info("------ RESPONSE DETAILS ------")

                # 1. Status Code
                app.logger.info(f"Status Code: {response.status_code}")

                # 2. Response Text (Raw text of the response)
                app.logger.info(f"Response Text: {response.text}")

                # 3. Try to log JSON response (if applicable)
                try:
                    json_data = response.json()
                    app.logger.info(f"Response JSON: {json_data}")
                except ValueError:
                    app.logger.info("Response is not in JSON format.")

                error_message = (
                    f"FusekiError: Failed to add graph to Fuseki"
                    + f"\nEr is iets misgegaan bij het toevoegen van de graph aan de triplestore. Controleer het bestand en probeer opnieuw. Mocht dit probleem zich blijven voordoen neem dan contact met het beheerteam via Modellenbibliotheek@belastingdienst.nl"
                )
                melding_manager.meld(ms.ERROR, error_message)
            else:
                app.logger.info(
                    f"Succesfully added <{named_graph_to_add}> in fuseki, returned: {response}"
                )
                # success_message = f"Succesfully added <{named_graph_to_add}> in fuseki, returned: {response}"
                # melding_manager.meld(ms.SUCCESS, success_message)

        except Exception as e:
            error_message = f"{e}: {traceback.format_exc()}"
            melding_manager.meld(ms.ERROR, error_message)

    def check_if_error_melding():
        error_meldingen = [
            melding for melding in melding_manager if melding.soort == ms.ERROR
        ]
        if error_meldingen:
            resultaat = ms.ERROR
            opmerking = error_meldingen[0].melding
            return (
                jsonify(
                    {
                        "resultaat": resultaat,
                        "opmerking": opmerking,
                        "py_acties": py_acties,
                        "titel": titel,
                    }
                ),
                200,
            )
        else:
            return ""

    # 1.
    try:
        synt_valid_graph = validate_syntactically_using_rdflib(file_content_bytes)
    except Exception as error_msg:
        melding_manager.meld(ms.ERROR, error_msg)
    error_melding = check_if_error_melding()
    if error_melding:
        return error_melding

    # 2.
    try:
        validate_semantically_using_pyshacl(synt_valid_graph)
    except Exception as error_msg:
        melding_manager.meld(ms.ERROR, error_msg)
    error_melding = check_if_error_melding()
    if error_melding:
        return error_melding

    # 3. put in PostgreSQL database
    try:
        put_in_postgreSQL(table=table, values=[file_content_bytes, file_name])
    except Exception:
        error_msg = f"\nUploaden kennisgebiedenregister mislukt.\nDe inhoud van het bestand '{file_name}' kon niet worden opgehaald. Mocht dit probleem zich blijven voordoen neem dan contact met het beheerteam via Modellenbibliotheek@belastingdienst.nl"
        melding_manager.meld(ms.ERROR, error_msg)
        return (
            jsonify(
                {
                    "resultaat": "ERROR",
                    "opmerking": error_msg,
                    "py_acties": py_acties,
                    "titel": titel,
                }
            ),
            200,
        )
    error_melding = check_if_error_melding()
    if error_melding:
        return error_melding

    # 4. Delete old graph in case it exists and add new one (doesnt return error if old not existing)
    try:
        fs.delete_graph(kennisgebiedenregister_uri)
    except Exception as error_msg:
        melding_manager.meld(ms.ERROR, error_msg)
    error_melding = check_if_error_melding()
    if error_melding:
        return error_melding

    # 5. Add new graph
    try:
        add_graph_fuseki_from_bytes(kennisgebiedenregister_uri, file_content_bytes)
    except Exception as error_msg:
        melding_manager.meld(ms.ERROR, error_msg)
    error_melding = check_if_error_melding()
    if error_melding:
        return error_melding

    success_msg = (
        f"Gegevens succesvol opgeslagen voor nieuw kennisgebiedenregister '{file_title}' met uri: '{kennisgebiedenregister_uri}'.",
    )

    # melding_manager.meld(ms.INFO, success_msg)
    return (
        jsonify(
            {
                "resultaat": resultaat,
                "opmerking": success_msg,
                "py_acties": py_acties,
                "titel": titel,
            }
        ),
        200,
    )


#####################################################################################################
# SINGLE NAAR MULTIPAGE
#####################################################################################################


@app.route("/over")
def render_over_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/over.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/domeinen")
def render_domeinen_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/domeinen.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/domein/<domein>")
def render_domein_pagina(domein: str):
    # Decode URL (voor %20, %2D enz)
    domein_decoded = urllib.parse.unquote(domein)

    # Alleen als er spaties of - in zitten, dan kunnen we het normaliseren
    if " " in domein_decoded or "-" in domein_decoded:
        woorden = domein_decoded.replace("-", " ").split()
        normalized_domein = "".join(woord.capitalize() for woord in woorden)

        if domein != normalized_domein:
            print(f"[INFO] Redirecting to: {normalized_domein}")
            return redirect(f"/domein/{normalized_domein}", code=301)
    else:
        # Geen spaties of '-', we nemen aan dat het al PascalCase is (of niet te fixen zonder lijst)
        normalized_domein = domein

    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    domein = urllib.parse.unquote(domein)
    print()
    return render_template(
        "pages/domein.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        domein=normalized_domein,
    )


@app.route("/registerwijzigingen")
def render_registerwijzigingen_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/registerwijzigingen.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/wijziging/<wijziging>")
def render_wijziging_pagina(wijziging: str):
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/wijziging.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        wijziging=wijziging,
    )


@app.route("/magazijn")
def render_magazijn_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/magazijn.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/kennisbronnen")
def render_kennisbronnen_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/kennisbronnen.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/kennisbanken")
def render_kennisbanken_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/kennisbanken.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/query_dienst")
def render_query_dienst_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/query_dienst.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/toetsing/<path:toetsing>")
def render_toetsing_pagina(toetsing: str):
    toetsing = unquote(toetsing)
    original_route = request.full_path

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/toetsing.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        toetsing=toetsing,
    )


@app.get("/toetsingsrapport/<path:subjecturi>")
def render_toetsingsrapport_pagina(subjecturi: str):
    subjecturi = unquote(subjecturi)
    # subjecturi = request.args.get("subjecturi")
    app.logger.info(f"subjecturi = {subjecturi}")

    print(subjecturi)

    original_route = request.full_path

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/toetsingsrapport.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        subjecturi=subjecturi,
    )


@app.get("/toetsingskader/<path:subjecturi>")
def render_toetsingskader_pagina(subjecturi: str):
    subjecturi = unquote(subjecturi)
    # subjecturi = request.args.get("subjecturi")
    app.logger.info(f"subjecturi = {subjecturi}")

    print(subjecturi)

    original_route = request.full_path

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/toetsingskader.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        subjecturi=subjecturi,
    )


@app.get("/model/<modeltype>")
def render_model_pagina(modeltype):

    modeltype = modeltype.upper()
    graph = request.args.get("graph")
    uri = request.args.get("uri")

    original_route = request.full_path

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/model.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        graph=graph,
        uri=uri,
        modeltype=modeltype,
    )


@app.get("/dashboard")
def render_dashboard_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/dashboard.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/dashboard/domein/<domein>")
def render_dashboard_per_domein_pagina(domein: str):
    # Decode URL (voor %20, %2D enz)
    domein_decoded = urllib.parse.unquote(domein)

    # Alleen als er spaties of - in zitten, dan kunnen we het normaliseren
    if " " in domein_decoded or "-" in domein_decoded:
        woorden = domein_decoded.replace("-", " ").split()
        normalized_domein = "".join(woord.capitalize() for woord in woorden)

        if domein != normalized_domein:
            print(f"[INFO] Redirecting to: {normalized_domein}")
            return redirect(f"/domein/{normalized_domein}", code=301)
    else:
        # Geen spaties of '-', we nemen aan dat het al PascalCase is (of niet te fixen zonder lijst)
        normalized_domein = domein

    original_route = request.full_path

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )

    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")
    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/dashboard_domein_info.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        domein=normalized_domein,
    )


@app.get("/dashboard-per-kennisgebied")
def render_dashboard_per_kennisgebied_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/dashboard_per_kennisgebied.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/dashboard-modellen-per-kennisgebied/<kennisgebied>")
def render_dashboard_modellen_per_kennisgebied_pagina(kennisgebied: str):
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/dashboard_modellen_per_kennisgebied.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        kennisgebied=kennisgebied,
    )


@app.get("/dashboard-actuele-administraties/<tab>")
def render_dashboard_actuele_administraties_pagina(tab="gepubliceerd"):
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/dashboard_actuele_administraties.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        selected_tab=tab,
    )


@app.get("/kennisgebiedenregister")
def render_kennisgebiedenregister_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/kennisgebiedenregister.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/kennisgebied/<kennisgebied>")
def render_kennisgebied_pagina(kennisgebied: str):
    # Decode URL (voor %20, %2D enz)
    kennisgebied_decoded = urllib.parse.unquote(kennisgebied)

    # Alleen als er spaties of - in zitten, dan kunnen we het normaliseren
    if " " in kennisgebied_decoded or "-" in kennisgebied_decoded:
        woorden = kennisgebied_decoded.replace("-", " ").split()
        normalized_kennisgebied = "".join(woord.capitalize() for woord in woorden)

        if kennisgebied != normalized_kennisgebied:
            print(f"[INFO] Redirecting to: {normalized_kennisgebied}")
            return redirect(f"/kennisgebied/{normalized_kennisgebied}", code=301)
    else:
        normalized_kennisgebied = kennisgebied

    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/kennisgebied.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
        kennisgebied=normalized_kennisgebied,
    )


@app.get("/toetsingslogboek")
def render_toetsingslogboek_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/toetsingslogboek.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/beheer_toetsingslogboek")
def render_beheer_toetsingslogboek_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/beheer_toetsingslogboek.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/beheer_kennisgebiedenregister")
def render_beheer_kennisgebiedenregister_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/beheer_kennisgebiedenregister.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/afgeronde_verzoeken")
def render_afgeronde_verzoeken_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/afgeronde_verzoeken.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/toetsingsmodellen")
def render_toetsingsmodellen_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/toetsingsmodellen.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/mijn_modelverzoeken")
def render_modelverzoeken_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/modelverzoeken.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.get("/modelverzoek_indienen")
def render_modelverzoek_indienen_pagina():
    original_route = request.full_path.replace("/", "")

    users_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-users.json"
        )
    )

    usermenu_mapping = json.load(
        open(
            os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            + os.sep
            + "frontend"
            + os.sep
            + "ep"
            + os.sep
            + "mb-json-groep.json"
        )
    )
    if username not in users_mapping.keys():
        usergroep = "dflt"
        app.logger.info(f"username {username} not in users_mapping.keys")

    else:
        usergroep = users_mapping[username]
        app.logger.info(f"usergroep = {usergroep}")

    usermenu = json.dumps(usermenu_mapping[usergroep][afn.USERMENU])
    usergroepnaam = usermenu_mapping[usergroep][afn.USERGROEPNAAM]

    return render_template(
        "pages/modelverzoek_indienen.html",
        usernaam=f"{username}",
        userid=username,
        usergroepnaam=usergroepnaam,
        usermenu=usermenu,
        parameters=original_route,
    )


@app.route("/home")
def home():
    return render_template("pages/home.html", page_title="Home1", current_page="home2")


@app.after_request
def add_cache_headers(response):
    """
    Adds cache headers to static assets (JS, CSS, images, fonts, SVG).
    Allows browsers to cache these files for 1 hour for faster load times.
    HTML is excluded to ensure pages always stay up to date.
    """

    ONE_HOUR = int(timedelta(hours=1).total_seconds())

    contentType = (response.headers.get("Content-Type") or "").lower()
    # Cache alleen “assets”; laat HTML met rust
    if any(x in contentType for x in ["javascript", "css", "font", "image", "svg"]):
        response.headers["Cache-Control"] = f"public, max-age={ONE_HOUR}, immutable"
    return response


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    app.run(debug=True, use_debugger=False, use_reloader=False)
