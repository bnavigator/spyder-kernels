# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""
Class that handles communications between Spyder kernel and frontend.

Comms transmit data in a list of buffers, and in a json-able dictionnary.
Here, we only support a buffer list with a single element.

The messages exchanged have the following msg_dict:

    ```
    msg_dict = {
        'spyder_msg_type': spyder_msg_type,
        'content': content,
    }
    ```

The buffer is generated by cloudpickle using `PICKLE_PROTOCOL = 2`.

To simplify the usage of messaging, we use a higher level function calling
mechanism:
    - The `remote_call` method returns a RemoteCallHandler object
    - By calling an attribute of this object, the call is sent to the other
      side of the comm.
    - If the `_wait_reply` is implemented, remote_call can be called with
      `blocking=True`, which will wait for a reply sent by the other side.

The messages exchanged are:
    - Function call (spyder_msg_type = 'remote_call'):
        - The content is a dictionnary {
            'call_name': The name of the function to be called,
            'call_id': uuid to match the request to a potential reply,
            'settings': A dictionnary of settings,
            }
        - The buffer encodes a dictionnary {
            'call_args': The function args,
            'call_kwargs': The function kwargs,
            }
    - If the 'settings' has `'blocking' =  True`, a reply is sent.
      (spyder_msg_type = 'blocking_call_reply'):
        - The buffer contains the return value of the function.
        - The 'content' is a dict with: {
                'is_error': a boolean indicating if the return value is an
                            exception to be raised.
                'call_id': The uuid from above,
                'call_name': The function name (mostly for debugging)
                }
"""

import cloudpickle
import logging
import sys
import uuid
import traceback

PY2 = sys.version[0] == '2'
logger = logging.getLogger(__name__)
# To be able to get and set variables between Python 2 and 3
PICKLE_PROTOCOL = 2
# Maximum length of a serialized buffer
MAX_SERIALIZED_LENGHT = 1e6


class CommError(RuntimeError):
    pass


class CommBase(object):
    """
    Class with the necessary attributes and methods to handle
    communications between a kernel and a frontend.
    Subclasses must open a comm and register it with `self._register_comm`.
    """

    def __init__(self):
        super(CommBase, self).__init__()
        self.calling_comm_id = None
        self._comms = {}
        self._message_handlers = {}
        self._remote_call_handlers = {}
        self._call_reply_dict = {}
        self._register_message_handler(
            'remote_call', self._handle_remote_call)
        self._register_message_handler(
            'blocking_call_reply', self._handle_blocking_call_reply)
        # Dummy functions for testing and to trigger side effects such as
        # an interruption or waiting for a reply.
        def pong_back():
            self.remote_call(self.calling_comm_id).pong()
        self.register_call_handler('ping', pong_back)
        self.register_call_handler('pong', lambda: None)

    def close(self, comm_id=None):
        """Close the comm and notify the other side."""
        if comm_id is None:
            # close all the comms
            id_list = list(self._comms.keys())
        else:
            id_list = [comm_id]

        for comm_id in id_list:
            self._comms[comm_id].close()
            del self._comms[comm_id]

    def is_open(self, comm_id=None):
        """Check to see if the comm is open."""
        if comm_id is None:
            return len(self._comms) > 0
        return comm_id in self._comms

    def register_call_handler(self, call_name, handler):
        """
        Register a remote call handler.

        Parameters
        ----------
        call_name : str
            The name of the called function.
        handler : callback
            A function to handle the request, or `None` to unregister
            `call_name`.
        """
        if not handler:
            self._remote_call_handlers.pop(call_name, None)
            return

        self._remote_call_handlers[call_name] = handler

    def remote_call(self, comm_id=None, **settings):
        """Get a handler for remote calls."""
        return RemoteCallFactory(self, comm_id, **settings)

    # ---- Private -----
    def _send_message(self, spyder_msg_type, content=None, data=None,
                      comm_id=None):
        """
        Publish custom messages to the other side.

        Parameters
        ----------
        spyder_msg_type: str
            The spyder message type
        content: dict
            The (JSONable) content of the message
        data: any
            Any object that is serializable by cloudpickle (should be most
            things). Will arrive as cloudpickled bytes in `.buffers[0]`.
        comm_id: int
            the comm to send to. If None sends to all comms.
        """

        if not self.is_open(comm_id):
            raise CommError("The comm is not connected.")
        import cloudpickle
        msg_dict = {
            'spyder_msg_type': spyder_msg_type,
            'content': content,
            }
        buffers = [cloudpickle.dumps(data, protocol=PICKLE_PROTOCOL)]
        # Needed to prevent memory leaks. See spyder-ide/spyder#7158.
        if len(buffers[0]) > MAX_SERIALIZED_LENGHT:
            raise RuntimeError("Maximum serialized length exceeded.")
        if comm_id is None:
            # send to all the comms
            id_list = list(self._comms.keys())
        else:
            id_list = [comm_id]
        for comm_id in id_list:
            self._comms[comm_id].send(msg_dict, buffers=buffers)

    @property
    def _comm_name(self):
        """
        Get the name used for the underlying comms.
        """
        return 'spyder_api'

    def _register_message_handler(self, message_id, handler):
        """
        Register a message handler.

        Parameters
        ----------
        message_id : str
            The identifier for the message
        handler : callback
            A function to handle the message. This is called with 3 arguments:
                - msg_dict: A dictionary with message information.
                - buffer: The data transmitted in the buffer
                - load_exception: Exception from buffer unpickling
            Pass None to unregister the message_id
        """
        if handler is None:
            self._message_handlers.pop(message_id, None)
            return

        self._message_handlers[message_id] = handler

    def _register_comm(self, comm):
        """
        Open a new comm to the kernel.
        """
        comm.on_msg(self._comm_message)
        comm.on_close(self._comm_close)
        self._comms[comm.comm_id] = comm

    def _comm_close(self, msg):
        """Close comm."""
        comm_id = msg['content']['comm_id']
        del self._comms[comm_id]

    def _comm_message(self, msg):
        """
        Handle internal spyder messages.
        """
        self.calling_comm_id = msg['content']['comm_id']
        # Load the buffer. Only one is supported.
        try:
            if PY2:
                buffer = cloudpickle.loads(msg['buffers'][0])
            else:
                buffer = cloudpickle.loads(bytes(msg['buffers'][0]))
            load_exception = None
        except Exception as e:
            load_exception = e
            buffer = None

        # Get message dict
        msg_dict = msg['content']['data']

        spyder_msg_type = msg_dict['spyder_msg_type']

        if spyder_msg_type in self._message_handlers:
            self._message_handlers[spyder_msg_type](
                msg_dict, buffer, load_exception)
            return

        logger.debug("No such spyder message type: %s" % spyder_msg_type)

    def _handle_remote_call(self, msg, buffer, load_exception):
        """Handle a remote call."""
        msg_dict = msg['content']
        if load_exception:
            logger.debug(
                "Exception in cloudpickle.loads : %s" % str(load_exception))
            return
        try:
            return_value = self._remote_callback(
                    msg_dict['call_name'],
                    buffer['call_args'],
                    buffer['call_kwargs'])
            self._set_call_return_value(msg_dict, return_value)
        except Exception as e:
            tb = traceback.extract_tb(sys.exc_info()[2])
            self._set_call_return_value(msg_dict, e, traceback=tb)

    def _remote_callback(self, call_name, call_args, call_kwargs):
        """Call the callback function for the remote call."""
        if call_name in self._remote_call_handlers:
            return self._remote_call_handlers[call_name](
                *call_args, **call_kwargs)

        raise CommError("No such spyder call type: %s" % call_name)

    def _set_call_return_value(self, call_dict, data, traceback=None):
        """
        A remote call has just been processed.

        This will reply if settings['blocking'] == True
        """
        settings = call_dict['settings']
        if 'blocking' not in settings or not settings['blocking']:
            return
        content = {
            'is_error': traceback is not None,
            'call_id': call_dict['call_id'],
            'call_name': call_dict['call_name']
        }
        if traceback is not None:
            data = [data, traceback]

        self._send_message('blocking_call_reply', content=content, data=data,
                           comm_id=self.calling_comm_id)

    def _get_call_return_value(self, call_dict):
        """
        A remote call has just been sent.

        If settings['blocking'] == True, this will wait for a reply and return
        the replied value.
        """
        settings = call_dict['settings']

        if 'blocking' not in settings or not settings['blocking']:
            return

        if 'timeout' in settings:
            timeout = settings['timeout']
        else:
            timeout = 3  # Seconds

        call_id = call_dict['call_id']
        call_name = call_dict['call_name']

        self._wait_reply(call_id, call_name, timeout)

        reply = self._call_reply_dict[call_id]
        self._call_reply_dict.pop(call_id)

        if reply['is_error']:
            error, tb = reply['value']
            traceback.print_list(tb)
            raise error

        return reply['value']

    def _wait_reply(self, call_id, call_name, timeout):
        """
        Wait for the other side reply.
        """
        raise NotImplementedError

    def _handle_blocking_call_reply(self, msg_dict, buffer, load_exception):
        """
        A blocking call received a reply.
        """
        content = msg_dict['content']
        request_id = content['call_id']
        if load_exception:
            buffer = load_exception
            handle_error = True
        else:
            handle_error = content['is_error']

        self._call_reply_dict[request_id] = {
                'is_error': handle_error,
                'value': buffer,
                'content': content
                }
        self._reply_recieved(request_id)

    def _reply_recieved(self, call_id):
        """A call got a reply."""
        return


class RemoteCallFactory(object):
    """Class to create `RemoteCall`s."""

    def __init__(self, comms_wrapper, comm_id, **settings):
        # Avoid setting attributes
        super(RemoteCallFactory, self).__setattr__(
            '_comms_wrapper', comms_wrapper)
        super(RemoteCallFactory, self).__setattr__('_comm_id', comm_id)
        super(RemoteCallFactory, self).__setattr__('_settings', settings)

    def __getattr__(self, name):
        """Get a call for a function named 'name'."""
        return RemoteCall(name, self._comms_wrapper, self._comm_id,
                          self._settings)

    def __setattr__(self, name, value):
        """Set an attribute to the other side."""
        raise NotImplementedError


class RemoteCall():
    """Class to call the other side of the comms like a function."""

    def __init__(self, name, comms_wrapper, comm_id, settings):
        self._name = name
        self._comms_wrapper = comms_wrapper
        self._comm_id = comm_id
        self._settings = settings

    def __call__(self, *args, **kwargs):
        """
        Transmit the call to the other side of the tunnel.

        The args and kwargs have to be picklable.
        """
        call_id = uuid.uuid4().hex
        call_dict = {
            'call_name': self._name,
            'call_id': call_id,
            'settings': self._settings,
            }
        call_data = {
            'call_args': args,
            'call_kwargs': kwargs,
            }

        if not self._comms_wrapper.is_open(self._comm_id):
            # Only an error if the call is blocking.
            if 'blocking' in self._settings and self._settings['blocking']:
                raise CommError("The comm is not connected.")
            logger.debug("Call to unconnected comm: %s" % self._name)
            return

        self._comms_wrapper._send_message(
            'remote_call', content=call_dict, data=call_data,
            comm_id=self._comm_id)
        return self._comms_wrapper._get_call_return_value(call_dict)
