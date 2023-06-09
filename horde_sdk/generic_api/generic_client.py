"""The API client which can perform arbitrary horde API requests."""

from typing import Generic, TypeVar

import aiohttp
import requests
from pydantic import BaseModel, ValidationError
from strenum import StrEnum

from horde_sdk.generic_api.apimodels import (
    BaseRequest,
    BaseRequestAuthenticated,
    BaseRequestUserSpecific,
    BaseRequestWorkerDriven,
    BaseResponse,
    RequestErrorResponse,
)
from horde_sdk.generic_api.metadata import (
    GenericAcceptTypes,
    GenericHeaderFields,
    GenericPathFields,
    GenericQueryFields,
)

HordeRequest = TypeVar("HordeRequest", bound=BaseRequest)
"""TypeVar for the request type."""
HordeResponse = TypeVar("HordeResponse", bound=BaseResponse)
"""TypeVar for the response type."""


class _ParsedRequest(BaseModel):
    endpoint_no_query: str
    """The endpoint URL without any query parameters."""
    request_headers: dict
    """The headers to be sent with the request."""
    request_queries: dict
    """The query parameters to be sent with the request."""
    request_params: dict
    """The path parameters to be sent with the request."""
    request_body: dict | None
    """The body to be sent with the request, or `None` if no body should be sent."""


class GenericHordeAPIClient:
    """Interfaces with any flask API the horde provides, intended to be fairly dynamic/flexible.

    You can use the friendly, typed functions, or if you prefer more control, you can use the `submit_request` method.
    """

    _header_data_keys: type[GenericHeaderFields] = GenericHeaderFields
    """A list of all fields which would appear in the API request header."""
    _path_data_keys: type[GenericPathFields] = GenericPathFields
    """A list of all fields which would appear in any API action path (appearing before the '?' as part of the URL)"""
    _query_data_keys: type[GenericQueryFields] = GenericQueryFields
    """A list of all fields which would appear in any API action query (appearing after the '?')"""

    _accept_types: type[GenericAcceptTypes] = GenericAcceptTypes
    """A list of all valid values for the header key 'accept'."""

    def __init__(
        self,
        *,
        header_fields: type[GenericHeaderFields] = GenericHeaderFields,
        path_fields: type[GenericPathFields] = GenericPathFields,
        query_fields: type[GenericQueryFields] = GenericQueryFields,
        accept_types: type[GenericAcceptTypes] = GenericAcceptTypes,
    ) -> None:
        """Initializes a new `GenericHordeAPIClient` instance.

        Args:
            header_fields (type[GenericHeaderFields], optional): Pass this to define the API's Header fields.
            Defaults to GenericHeaderFields.

            path_fields (type[GenericPathFields], optional): Pass this to define the API's URL path fields.
            Defaults to GenericPathFields.

            query_fields (type[GenericQueryFields], optional): Pass this to define the API's URL query fields.
            Defaults to GenericQueryFields.

            accept_types (type[GenericAcceptTypes], optional): Pass this to define the API's accept types.
            Defaults to GenericAcceptTypes.

        Raises:
            TypeError: If any of the passed types are not subclasses of their respective `Generic*` class.
        """ """"""
        if not issubclass(header_fields, GenericHeaderFields):
            raise TypeError("`headerData` must be of type `GenericHeaderData` or a subclass of it!")
        if not issubclass(path_fields, GenericPathFields):
            raise TypeError("`pathData` must be of type `GenericPathData` or a subclass of it!")
        if not issubclass(accept_types, GenericAcceptTypes):
            raise TypeError("`acceptTypes` must be of type `GenericAcceptTypes` or a subclass of it!")
        if not issubclass(query_fields, GenericQueryFields):
            raise TypeError("`queryData` must be of type `GenericQueryData` or a subclass of it!")

        self._header_data_keys = header_fields
        self._path_data_keys = path_fields
        self._query_data_keys = query_fields
        self._accept_types = accept_types

    def _validate_and_prepare_request(self, api_request: BaseRequest) -> _ParsedRequest:
        """Validates the given `api_request` and returns a `_ParsedRequest` instance with the data to be sent.

        The thrust of the method is to convert a `BaseRequest` instance into the data needed to make a request with
        `requests`.
        """
        if not issubclass(api_request.__class__, BaseRequest):
            raise TypeError("`request` must be of type `BaseRequest` or a subclass of it!")

        def get_specified_data_keys(data_keys: type[StrEnum], api_request: BaseRequest) -> dict[str, str]:
            return {
                py_field_name: str(api_field_name)
                for py_field_name, api_field_name in data_keys._member_map_.items()
                if hasattr(api_request, py_field_name) and getattr(api_request, py_field_name) is not None
            }

        specified_headers = get_specified_data_keys(self._header_data_keys, api_request)
        specified_paths = get_specified_data_keys(self._path_data_keys, api_request)
        specified_queries = get_specified_data_keys(self._query_data_keys, api_request)

        endpoint_url: str = api_request.get_endpoint_url()

        for py_field_name, api_field_name in specified_paths.items():
            # Replace the path key with the value from the request
            # IE: /v2/ratings/{id} -> /v2/ratings/123
            endpoint_url = endpoint_url.format_map({api_field_name: str(getattr(api_request, py_field_name))})

        extra_header_keys: list[str] = api_request.get_header_fields()

        request_params_dict: dict[str, object] = {}
        request_headers_dict: dict[str, object] = {}
        request_queries_dict: dict[str, object] = {}

        for request_key, request_value in api_request.__dict__.items():
            if request_key in specified_paths:
                continue
            if request_key in specified_headers:
                request_headers_dict[request_key] = request_value
                continue
            if request_key in extra_header_keys:
                # Remove any trailing underscores from the key as they are used to avoid python keyword conflicts
                api_name = request_key if not request_key.endswith("_") else request_key[:-1]
                specified_headers[request_key] = api_name
                request_headers_dict[request_key] = request_value

                continue
            if request_key in specified_queries:
                request_queries_dict[request_key] = request_value
                continue

            request_params_dict[request_key] = request_value

        all_fields_to_exclude_from_body = set(
            list(specified_headers.keys())
            + list(specified_paths.keys())
            + list(specified_queries.keys())
            + extra_header_keys,
        )
        request_body_data_dict: dict | None = api_request.model_dump(
            exclude_none=True,
            exclude_unset=True,
            exclude=all_fields_to_exclude_from_body,
        )

        if request_body_data_dict == {}:
            request_body_data_dict = None

        return _ParsedRequest(
            endpoint_no_query=endpoint_url,
            request_headers=request_headers_dict,
            request_queries=request_queries_dict,
            request_params=request_params_dict,
            request_body=request_body_data_dict,
        )

    def _after_request_handling(
        self,
        api_request: BaseRequest,
        raw_response: requests.Response,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        expected_response_type = api_request.get_success_response_type()
        raw_response_json = raw_response.json()

        # If requests response is a failure code, see if a `message` key exists in the response.
        # If so, return a RequestErrorResponse
        if raw_response.status_code >= 400:
            if len(raw_response_json) == 1 and "message" in raw_response_json:
                return RequestErrorResponse(**raw_response_json)

            raise RuntimeError(
                (
                    "Received a non-200 response code, but no `message` key was found "
                    f"in the response: {raw_response_json}"
                ),
            )

        handled_response: HordeResponse | RequestErrorResponse | None = None
        try:
            parsed_response = expected_response_type.from_dict_or_array(raw_response_json)
            if isinstance(parsed_response, expected_response):
                handled_response = parsed_response
            else:
                handled_response = RequestErrorResponse(
                    message="The response type doesn't match expected one! See `object_data` for the raw response.",
                    object_data={"raw_response": raw_response_json},
                )
        except ValidationError as e:
            if not isinstance(handled_response, expected_response):
                error_response = RequestErrorResponse(
                    message="The response type doesn't match expected one! See `object_data` for the raw response.",
                    object_data={"exception": e, "raw_response": raw_response_json},
                )
                handled_response = error_response

        return handled_response

    def submit_request(
        self,
        api_request: BaseRequest,
        expected_response_type: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Submit a request to the API and return the response.

        Automatically determines the correct method to call based on calling `.get_http_method()` on the request.

        If you are wondering why `expected_response` is a parameter, it is because the API may return different
        responses depending on the payload or other factors. It is up to you to determine which response type you
        expect, and pass it in here.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        http_method_name = api_request.get_http_method()._value_.lower()
        api_client_method = getattr(self, http_method_name)
        return api_client_method(api_request, expected_response_type)

    async def async_submit_request(
        self,
        api_request: BaseRequest,
        expected_response_type: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Submit a request to the API asynchronously and return the response.

        Automatically determines the correct method to call based on calling `.get_http_method()` on the request.

        If you are wondering why `expected_response` is a parameter, it is because the API may return different
        responses depending on the payload or other factors. It is up to you to determine which response type you
        expect, and pass it in here.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        http_method_name = api_request.get_http_method()._value_.lower()
        api_client_method = getattr(self, f"async_{http_method_name}")
        return await api_client_method(api_request, expected_response_type)

    def get(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a GET request to the API.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        if parsed_request.request_body is not None:
            raise RuntimeError(
                "GET requests cannot have a body! This probably means you forgot to override `get_header_fields()`",
            )
        raw_response = requests.get(
            parsed_request.endpoint_no_query,
            headers=parsed_request.request_headers,
            params=parsed_request.request_queries,
            allow_redirects=True,
        )

        return self._after_request_handling(api_request, raw_response, expected_response)

    async def async_get(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a GET request to the API asynchronously.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        if parsed_request.request_body is not None:
            raise RuntimeError("GET requests cannot have a body!")

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                parsed_request.endpoint_no_query,
                headers=parsed_request.request_headers,
                params=parsed_request.request_queries,
                allow_redirects=True,
            ) as response,
        ):
            raw_response = await response.json()

        return self._after_request_handling(api_request, raw_response, expected_response)

    def post(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a POST request to the API.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        raw_response = requests.post(
            parsed_request.endpoint_no_query,
            headers=parsed_request.request_headers,
            params=parsed_request.request_queries,
            json=parsed_request.request_body,
            allow_redirects=True,
        )

        return self._after_request_handling(api_request, raw_response, expected_response)

    async def async_post(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a POST request to the API asynchronously.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                parsed_request.endpoint_no_query,
                headers=parsed_request.request_headers,
                params=parsed_request.request_queries,
                json=parsed_request.request_body,
                allow_redirects=True,
            ) as response,
        ):
            raw_response = await response.json()

        return self._after_request_handling(api_request, raw_response, expected_response)

    def put(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a PUT request to the API.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        raw_response = requests.put(
            parsed_request.endpoint_no_query,
            headers=parsed_request.request_headers,
            params=parsed_request.request_queries,
            json=parsed_request.request_body,
            allow_redirects=True,
        )

        return self._after_request_handling(api_request, raw_response, expected_response)

    async def async_put(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a PUT request to the API asynchronously.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)

        async with (
            aiohttp.ClientSession() as session,
            session.put(
                parsed_request.endpoint_no_query,
                headers=parsed_request.request_headers,
                params=parsed_request.request_queries,
                json=parsed_request.request_body,
                allow_redirects=True,
            ) as response,
        ):
            raw_response = await response.json()

        return self._after_request_handling(api_request, raw_response, expected_response)

    def patch(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a PATCH request to the API.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        raw_response = requests.patch(
            parsed_request.endpoint_no_query,
            headers=parsed_request.request_headers,
            params=parsed_request.request_queries,
            json=parsed_request.request_body,
            allow_redirects=True,
        )

        return self._after_request_handling(api_request, raw_response, expected_response)

    async def async_patch(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a PATCH request to the API asynchronously.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)

        async with (
            aiohttp.ClientSession() as session,
            session.patch(
                parsed_request.endpoint_no_query,
                headers=parsed_request.request_headers,
                params=parsed_request.request_queries,
                json=parsed_request.request_body,
                allow_redirects=True,
            ) as response,
        ):
            raw_response = await response.json()

        return self._after_request_handling(api_request, raw_response, expected_response)

    def delete(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a DELETE request to the API.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)
        raw_response = requests.delete(
            parsed_request.endpoint_no_query,
            headers=parsed_request.request_headers,
            params=parsed_request.request_queries,
            json=parsed_request.request_body,
            allow_redirects=True,
        )

        return self._after_request_handling(api_request, raw_response, expected_response)

    async def async_delete(
        self,
        api_request: BaseRequest,
        expected_response: type[HordeResponse],
    ) -> HordeResponse | RequestErrorResponse:
        """Perform a DELETE request to the API asynchronously.

        Args:
            api_request (BaseRequest): The request to submit.
            expected_response (type[HordeResponse]): The expected response type.

        Returns:
            HordeResponse | RequestErrorResponse: The response from the API.
        """
        parsed_request = self._validate_and_prepare_request(api_request)

        async with (
            aiohttp.ClientSession() as session,
            session.delete(
                parsed_request.endpoint_no_query,
                headers=parsed_request.request_headers,
                params=parsed_request.request_queries,
                json=parsed_request.request_body,
                allow_redirects=True,
            ) as response,
        ):
            raw_response = await response.json()

        return self._after_request_handling(api_request, raw_response, expected_response)


class HordeRequestHandler(Generic[HordeRequest, HordeResponse]):
    request: HordeRequest
    """The request to be handled."""

    response: HordeResponse | RequestErrorResponse
    """The response from the API."""

    def __init__(self, request: HordeRequest) -> None:
        self.request = request

    def __enter__(self) -> HordeRequest:
        return self.request

    def __exit__(self, exc_type: type[Exception], exc_val: Exception, exc_tb: object) -> None:
        if exc_type is not None:
            print(f"Error: {exc_val}, Type: {exc_type}, Traceback: {exc_tb}")
            if not self.request.is_recovery_enabled():
                return

            recovery_request_type = self.request.get_recovery_request_type()

            request_params = {}

            mappable_base_types: list[type[BaseModel]] = [
                BaseRequestAuthenticated,
                BaseRequestUserSpecific,
                BaseRequestWorkerDriven,
            ]

            # If it any of the base types are a subclass of the recovery request type, then we can map the request
            # parameters to the recovery request.
            #
            # For example, if the recovery request type is `DeleteImageGenerateRequest`, and the request is
            # `ImageGenerateAsyncRequest`, then we can map the `id` parameter from the request to the `id` parameter
            # of the recovery request.
            for base_type in mappable_base_types:
                if issubclass(recovery_request_type, base_type):
                    for key in base_type.model_fields:
                        request_params[key] = getattr(self.request, key)
