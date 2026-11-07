"""Keep native LangChain serialization; observe usage before message parsing."""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import (
    _response_to_result, _handle_client_error, _handle_server_error,
)
from google.genai.errors import ClientError, ServerError

from app.llm.receipts import capture_native_response, capture_result


class ObservedChatAnthropic(ChatAnthropic):
    def _format_output(self, data, **kwargs):
        usage = capture_native_response(data, "anthropic")
        result = super()._format_output(data, **kwargs)
        capture_result(result, usage)
        return result


class ObservedChatGoogle(ChatGoogleGenerativeAI):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        request = self._prepare_request(messages, stop=stop, **kwargs)
        try:
            response = await self.async_client.models.generate_content(**request)
        except ClientError as exc:
            _handle_client_error(exc, request)
        except ServerError as exc:
            _handle_server_error(exc)
        usage = capture_native_response(response, "google")
        result = _response_to_result(response)
        capture_result(result, usage)
        return result
