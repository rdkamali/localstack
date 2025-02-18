import base64
import json
import logging
from abc import ABC, abstractmethod
from http import HTTPStatus

from flask import Response as FlaskResponse
from requests import Response

from localstack import config
from localstack.constants import APPLICATION_JSON, HEADER_CONTENT_TYPE
from localstack.services.apigateway import helpers
from localstack.services.apigateway.context import ApiInvocationContext
from localstack.services.apigateway.helpers import (
    extract_path_params,
    extract_query_string_params,
    get_event_request_context,
)
from localstack.services.apigateway.templates import (
    MappingTemplates,
    RequestTemplates,
    ResponseTemplates,
)
from localstack.services.awslambda import lambda_api
from localstack.services.stepfunctions.stepfunctions_utils import await_sfn_execution_result
from localstack.utils import common
from localstack.utils.aws import aws_stack
from localstack.utils.aws.aws_responses import (
    LambdaResponse,
    flask_to_requests_response,
    requests_response,
)
from localstack.utils.collections import remove_attributes
from localstack.utils.common import make_http_request, to_str
from localstack.utils.json import json_safe
from localstack.utils.strings import camel_to_snake_case, to_bytes

LOG = logging.getLogger(__name__)


class BackendIntegration(ABC):
    """Abstract base class representing a backend integration"""

    def __init__(self):
        self.request_templates = RequestTemplates()
        self.response_templates = ResponseTemplates()

    @abstractmethod
    def invoke(self, invocation_context: ApiInvocationContext):
        pass

    @classmethod
    def _create_response(cls, status_code, headers, data=""):
        response = Response()
        response.status_code = status_code
        response.headers = headers
        response._content = data
        return response

    @classmethod
    def apply_response_parameters(
        cls, invocation_context: ApiInvocationContext, response: Response
    ):
        integration = invocation_context.integration
        integration_responses = integration.get("integrationResponses") or {}
        if not integration_responses:
            return response
        entries = list(integration_responses.keys())
        return_code = str(response.status_code)
        if return_code not in entries:
            if len(entries) > 1:
                LOG.info("Found multiple integration response status codes: %s", entries)
                return response
            return_code = entries[0]
        response_params = integration_responses[return_code].get("responseParameters", {})
        for key, value in response_params.items():
            # TODO: add support for method.response.body, etc ...
            if str(key).lower().startswith("method.response.header."):
                header_name = key[len("method.response.header.") :]
                response.headers[header_name] = value.strip("'")
        return response


class SnsIntegration(BackendIntegration):
    def invoke(self, invocation_context: ApiInvocationContext) -> Response:
        invocation_context.context = get_event_request_context(invocation_context)
        try:
            payload = self.request_templates.render(invocation_context)
        except Exception as e:
            LOG.warning("Failed to apply template for SNS integration", e)
            raise
        uri = (
            invocation_context.integration.get("uri")
            or invocation_context.integration.get("integrationUri")
            or ""
        )
        region_name = uri.split(":")[3]
        headers = aws_stack.mock_aws_request_headers(service="sns", region_name=region_name)
        return make_http_request(
            config.service_url("sns"), method="POST", headers=headers, data=payload
        )


class LambdaProxyIntegration(BackendIntegration):
    @classmethod
    def update_content_length(cls, response: Response):
        if response and response.content is not None:
            response.headers["Content-Length"] = str(len(response.content))

    @classmethod
    def lambda_result_to_response(cls, result) -> LambdaResponse:
        response = LambdaResponse()
        response.headers.update({"content-type": "application/json"})
        parsed_result = result if isinstance(result, dict) else json.loads(str(result or "{}"))
        parsed_result = common.json_safe(parsed_result)
        parsed_result = {} if parsed_result is None else parsed_result

        keys = parsed_result.keys()
        if not ("statusCode" in keys and "body" in keys):
            LOG.warning(
                'Lambda output should follow the next JSON format: { "isBase64Encoded": true|false, "statusCode": httpStatusCode, "headers": { "headerName": "headerValue", ... },"body": "..."}'
            )
            response.status_code = 502
            response._content = json.dumps({"message": "Internal server error"})
            return response

        response.status_code = int(parsed_result.get("statusCode", 200))
        parsed_headers = parsed_result.get("headers", {})
        if parsed_headers is not None:
            response.headers.update(parsed_headers)
        try:
            result_body = parsed_result.get("body")
            if isinstance(result_body, dict):
                response._content = json.dumps(result_body)
            else:
                body_bytes = to_bytes(to_str(result_body or ""))
                if parsed_result.get("isBase64Encoded", False):
                    body_bytes = base64.b64decode(body_bytes)
                response._content = body_bytes
        except Exception as e:
            LOG.warning("Couldn't set Lambda response content: %s", e)
            response._content = "{}"
        response.multi_value_headers = parsed_result.get("multiValueHeaders") or {}
        return response

    def invoke(self, invocation_context: ApiInvocationContext):
        uri = (
            invocation_context.integration.get("uri")
            or invocation_context.integration.get("integrationUri")
            or ""
        )
        relative_path, query_string_params = extract_query_string_params(
            path=invocation_context.path_with_query_string
        )
        api_id = invocation_context.api_id
        stage = invocation_context.stage
        headers = invocation_context.headers
        resource_path = invocation_context.resource_path
        invocation_context.context = get_event_request_context(invocation_context)
        try:
            path_params = extract_path_params(path=relative_path, extracted_path=resource_path)
            invocation_context.path_params = path_params
        except Exception:
            path_params = {}

        func_arn = uri
        if ":lambda:path" in uri:
            func_arn = uri.split(":lambda:path")[1].split("functions/")[1].split("/invocations")[0]

        if invocation_context.authorizer_type:
            invocation_context.context["authorizer"] = invocation_context.auth_context

        payload = self.request_templates.render(invocation_context)

        # TODO: change this signature to InvocationContext as well!
        result = lambda_api.process_apigateway_invocation(
            func_arn,
            relative_path,
            payload,
            stage,
            api_id,
            headers,
            is_base64_encoded=invocation_context.is_data_base64_encoded,
            path_params=path_params,
            query_string_params=query_string_params,
            method=invocation_context.method,
            resource_path=resource_path,
            request_context=invocation_context.context,
            stage_variables=invocation_context.stage_variables,
        )

        if isinstance(result, FlaskResponse):
            response = flask_to_requests_response(result)
        elif isinstance(result, Response):
            response = result
        else:
            response = LambdaResponse()
            response.headers.update({"content-type": "application/json"})
            parsed_result = result if isinstance(result, dict) else json.loads(str(result or "{}"))
            parsed_result = common.json_safe(parsed_result)
            parsed_result = {} if parsed_result is None else parsed_result

            keys = parsed_result.keys()

            if not ("statusCode" in keys and "body" in keys):
                LOG.warning(
                    'Lambda output should follow the next JSON format: { "isBase64Encoded": true|false, "statusCode": httpStatusCode, "headers": { "headerName": "headerValue", ... },"body": "..."}'
                )
                response.status_code = 502
                response._content = json.dumps({"message": "Internal server error"})
                return response

            response.status_code = int(parsed_result.get("statusCode", 200))
            parsed_headers = parsed_result.get("headers", {})
            if parsed_headers is not None:
                response.headers.update(parsed_headers)
            try:
                result_body = parsed_result.get("body")
                if isinstance(result_body, dict):
                    response._content = json.dumps(result_body)
                else:
                    body_bytes = to_bytes(to_str(result_body or ""))
                    if parsed_result.get("isBase64Encoded", False):
                        body_bytes = base64.b64decode(body_bytes)
                    response._content = body_bytes
            except Exception as e:
                LOG.warning("Couldn't set Lambda response content: %s", e)
                response._content = "{}"
            response.multi_value_headers = parsed_result.get("multiValueHeaders") or {}

        # apply custom response template
        self.update_content_length(response)
        invocation_context.response = response

        self.response_templates.render(invocation_context)
        return invocation_context.response


class LambdaIntegration(BackendIntegration):
    def invoke(self, invocation_context: ApiInvocationContext):
        uri = (
            invocation_context.integration.get("uri")
            or invocation_context.integration.get("integrationUri")
            or ""
        )
        func_arn = uri
        if ":lambda:path" in uri:
            func_arn = uri.split(":lambda:path")[1].split("functions/")[1].split("/invocations")[0]

        headers = helpers.create_invocation_headers(invocation_context)
        invocation_context.context = helpers.get_event_request_context(invocation_context)
        invocation_context.stage_variables = helpers.get_stage_variables(invocation_context)
        if invocation_context.authorizer_type:
            invocation_context.context["authorizer"] = invocation_context.auth_context

        request_templates = RequestTemplates()
        payload = request_templates.render(invocation_context)

        invocation_result = lambda_api.run_lambda(
            func_arn=func_arn,
            event=payload,
        )
        result = invocation_result.result

        if isinstance(result, FlaskResponse):
            response = flask_to_requests_response(result)
        elif isinstance(result, Response):
            response = result
        else:
            response = LambdaResponse()

            is_async = headers.get("X-Amz-Invocation-Type", "").strip("'") == "Event"

            if is_async:
                response._content = ""
                response.status_code = 200
            else:
                # depending on the lambda executor sometimes it returns a string and sometimes a dict
                match result:
                    case dict():
                        parsed_result = result
                    case str():
                        # try to parse the result as json, if it succeeds we assume it's a valid
                        # json string and we don't do anything.
                        if isinstance(json.loads(result or "{}"), dict):
                            parsed_result = result
                        else:
                            # the docker executor returns a string wrapping a json string,
                            # so we need to remove the outer string
                            parsed_result = json.loads(result or "{}")
                    case _:
                        parsed_result = json.loads(str(result or "{}"))

                parsed_result = common.json_safe(parsed_result)
                parsed_result = {} if parsed_result is None else parsed_result
                response.status_code = 200
                response._content = parsed_result

        # apply custom response template
        invocation_context.response = response

        response_templates = ResponseTemplates()
        response_templates.render(invocation_context)
        invocation_context.response.headers["Content-Length"] = str(len(response.content or ""))
        return invocation_context.response


class MockIntegration(BackendIntegration):
    @classmethod
    def check_passthrough_behavior(cls, passthrough_behavior: str, request_template: str):
        return MappingTemplates(passthrough_behavior).check_passthrough_behavior(request_template)

    def invoke(self, invocation_context: ApiInvocationContext) -> Response:
        passthrough_behavior = invocation_context.integration.get("passthroughBehavior") or ""
        request_template = invocation_context.integration.get("requestTemplates", {}).get(
            invocation_context.headers.get(HEADER_CONTENT_TYPE)
        )

        # based on the configured passthrough behavior and the existence of template or not,
        # we proceed calling the integration or raise an exception.
        try:
            self.check_passthrough_behavior(passthrough_behavior, request_template)
        except MappingTemplates.UnsupportedMediaType:
            return MockIntegration._create_response(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value,
                headers={"Content-Type": APPLICATION_JSON},
                data=json.dumps({"message": f"{HTTPStatus.UNSUPPORTED_MEDIA_TYPE.phrase}"}),
            )

        # request template rendering
        request_payload = self.request_templates.render(invocation_context)

        # mapping is done based on "statusCode" field
        status_code = 200
        if invocation_context.headers.get(HEADER_CONTENT_TYPE) == APPLICATION_JSON:
            try:
                mock_response = json.loads(request_payload)
                status_code = mock_response.get("statusCode", status_code)
            except Exception as e:
                LOG.warning("failed to deserialize request payload after transformation: %s", e)
                http_status = HTTPStatus(500)
                return MockIntegration._create_response(
                    http_status.value,
                    headers={"Content-Type": APPLICATION_JSON},
                    data=json.dumps({"message": f"{http_status.phrase}"}),
                )

        # response template
        response = MockIntegration._create_response(
            status_code, invocation_context.headers, data=request_payload
        )
        response._content = self.response_templates.render(invocation_context, response=response)
        # apply response parameters
        response = self.apply_response_parameters(invocation_context, response)
        if not invocation_context.headers.get(HEADER_CONTENT_TYPE):
            invocation_context.headers.update({HEADER_CONTENT_TYPE: APPLICATION_JSON})
        return response


class StepFunctionIntegration(BackendIntegration):
    def invoke(self, invocation_context: ApiInvocationContext):
        uri = (
            invocation_context.integration.get("uri")
            or invocation_context.integration.get("integrationUri")
            or ""
        )
        action = uri.split("/")[-1]

        if APPLICATION_JSON in invocation_context.integration.get("requestTemplates", {}):
            payload = self.request_templates.render(invocation_context)
            payload = json.loads(payload)
        else:
            payload = json.loads(invocation_context.data)

        client = aws_stack.connect_to_service("stepfunctions")
        if isinstance(payload.get("input"), dict):
            payload["input"] = json.dumps(payload["input"])

        # Hot fix since step functions local package responses: Unsupported Operation: 'StartSyncExecution'
        method_name = (
            camel_to_snake_case(action) if action != "StartSyncExecution" else "start_execution"
        )

        try:
            method = getattr(client, method_name)
        except AttributeError:
            msg = f"Invalid step function action: {method_name}"
            LOG.error(msg)
            return StepFunctionIntegration._create_response(
                HTTPStatus.BAD_REQUEST.value,
                headers={"Content-Type": APPLICATION_JSON},
                data=json.dumps({"message": msg}),
            )

        result = method(**payload)
        result = json_safe(remove_attributes(result, "ResponseMetadata"))
        response = StepFunctionIntegration._create_response(
            HTTPStatus.OK.value, aws_stack.mock_aws_request_headers(), data=result
        )
        if action == "StartSyncExecution":
            # poll for the execution result and return it
            result = await_sfn_execution_result(result["executionArn"])
            result_status = result.get("status")
            if result_status != "SUCCEEDED":
                return StepFunctionIntegration._create_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR.value,
                    headers={"Content-Type": APPLICATION_JSON},
                    data=json.dumps(
                        {
                            "message": "StepFunctions execution %s failed with status '%s'"
                            % (result["executionArn"], result_status)
                        }
                    ),
                )

            result = json_safe(result)
            response = requests_response(content=result)

        # apply response templates
        invocation_context.response = response
        response._content = self.response_templates.render(invocation_context)
        return response
