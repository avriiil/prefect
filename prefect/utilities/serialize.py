import base64
import datetime
import hashlib
import importlib
import inspect
import json
import types

import cloudpickle
import dateutil.parser
import wrapt
from Crypto import Random
from Crypto.Cipher import AES

import prefect

__all__ = [
    'JSONSerializable',
    'json_serializeable',
    'AESCipher',
    'serialize',
    'deserialize',
]

JSON_REGISTRY = dict()


def serialized_name(obj):
    # types and functions are stored as qualified names
    if isinstance(obj, (type, types.FunctionType)):
        return obj.__module__ + '.' + obj.__name__
    # instances are stored as types
    else:
        return serialized_name(type(obj))


class JSONSerializableMetaclass(type):

    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)
        JSON_REGISTRY[serialized_name(cls)] = cls
        return cls


class JSONSerializable(metaclass=JSONSerializableMetaclass):
    """
    A class that can automatically be serialized to JSON and deserialized later.

    When the class type is created, it registers itself so that Prefect knows
    how to instantiate it. When an instance is created, it records the arguments
    that were used to create it. On deserialization, the correct class type is
    instantiated with those same arguments.
    """

    __serialize_fields__ = []
    __serialize_class__ = True

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)

        self = object()
        signature = inspect.signature(cls.__init__)
        bound = signature.bind(self, *args, **kwargs)

        # identify the argument corresponding to 'self'
        self_arg = next(
            (k for k, a in bound.arguments.items() if a is self), None)
        # identify the argument corresponding to **kwargs
        kw_arg = next(
            (
                k for k, p in signature.parameters.items()
                if p.kind == p.VAR_KEYWORD), None)
        # identify the argument corresponding to *args
        pos_arg = next(
            (
                k for k, p in signature.parameters.items()
                if p.kind == p.VAR_POSITIONAL), None)
        if pos_arg:
            raise TypeError(
                'JSONSerializable classes do not support *args in __init__, '
                'because all arguments must be serializable with a known '
                'keyword. Consider replacing *args with an explicit sequence.')

        callargs = {}
        bound.arguments.pop(self_arg)
        # store this anyway, one day we might use it
        callargs['*args'] = bound.arguments.pop(pos_arg, ())
        callargs['**kwargs'] = bound.arguments.pop(kw_arg, {})
        callargs.update(bound.arguments)

        instance._init_args = callargs
        return instance

    def __init__(self):
        pass

    def __json__(self):
        serialized = {f: getattr(self, f) for f in self.__serialize_fields__}
        if self.__serialize_class__:
            serialized.update(
                {
                    '__prefect_type__': serialized_name(self),
                    '__prefect_version__': prefect.__version__
                })
            if self._init_args.get('*args') or self._init_args.get('**kwargs'):
                serialized['__prefect_args__'] = self._init_args

        return serialized

    def serialize(self):
        return json.dumps(self)

    @classmethod
    def deserialize(cls, serialized):
        if serialized.get('__prefect_type__') == serialized_name(cls):
            return json.loads(serialized)
        else:
            raise TypeError('Invalid serialization type.')


def json_serializable(fn):
    """
    Decorator for marking a function as serializable.

    Note that this works by importing the function at the time of
    deserialization, so it may not be stable across software versions.
    """
    fn.__json__ = lambda: {'__function__': serialized_name(fn)}
    return fn


def patch_json():
    """
    Monkey-patches the builtin JSON library with the following features:

    ENCODING:
        - Any class with a __json__() method is stored as the result of
            calling that method
        - DateTimes are stored as timestamps with the key '__datetime__'
        - TimeDeltas are stored as total seconds with the key '__timedelta__'

    DECODING:
        - Prefect objects are instantiated correctly
        - '__datetime__' keys are restored as DateTimes
        - '__timedelta__' keys are restored as TimeDeltas
        - '__function__' keys are restored by importing the referenced object

    """

    _encode_json_original = json.JSONEncoder.default

    def encode_json(self, obj):

        # call __json__ method
        if hasattr(obj, '__json__'):
            return obj.__json__()

        # encode datetimes
        elif isinstance(obj, datetime.datetime):
            return {'__datetime__': obj.isoformat()}

        elif isinstance(obj, datetime.timedelta):
            return {'__timedelta__': obj.total_seconds()}

        # fallback on default
        else:
            return _encode_json_original(self, obj)

    json.JSONEncoder.default = encode_json

    def _default_object_hook(dct):
        # decode datetimes
        if '__datetime__' in dct:
            return dateutil.parser.parse(dct['__datetime__'])

        # decode timestamps
        if '__timedelta__' in dct:
            return datetime.timedelta(seconds=dct['__timedelta__'])

        # decode functions
        if '__function__' in dct:
            module, name = dct['__function__'].rsplit('.', 1)
            return getattr(importlib.import_module(module), name)

        # decode Prefect objects
        if '__prefect_type__' in dct:
            cls = JSON_REGISTRY[dct.pop('__prefect_type__')]
            callargs = dct.get('__prefect_args__', {})
            args = callargs.pop('*args', ())
            kwargs = callargs.pop('**kwargs', {})
            return cls(**callargs, **kwargs)
        return dct

    json._default_decoder = json.JSONDecoder(object_hook=_default_object_hook)


# call
patch_json()

# -----------------------------------------------------------------------------


class AESCipher(object):
    """
    http://stackoverflow.com/a/21928790
    """

    def __init__(self, key):
        self.bs = 32
        self.key = hashlib.sha256(key.encode()).digest()

    def encrypt(self, raw):
        raw = self._pad(raw)
        iv = Random.new().read(AES.block_size)
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return base64.b64encode(iv + cipher.encrypt(raw))

    def decrypt(self, enc):
        enc = base64.b64decode(enc)
        iv = enc[:AES.block_size]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return self._unpad(cipher.decrypt(enc[AES.block_size:])).decode('utf-8')

    def _pad(self, s):
        pad_chr = chr(self.bs - len(s) % self.bs)
        if isinstance(s, bytes):
            pad_chr = pad_chr.encode()
        return s + (self.bs - len(s) % self.bs) * pad_chr

    @staticmethod
    def _unpad(s):
        return s[:-ord(s[len(s) - 1:])]


def serialize(obj, encryption_key=None):
    """
    Serialize a Python object, optionally encrypting the result with a key

    Args:
        obj (object): The object to serialize
        encryption_key (str): key used to encrypt the serialization
    """
    if encryption_key is None:
        encryption_key = prefect.config.get('prefect', 'encryption_key')
    serialized = base64.b64encode(cloudpickle.dumps(obj))
    if encryption_key:
        cipher = AESCipher(key=encryption_key)
        serialized = cipher.encrypt(serialized)
    if isinstance(serialized, bytes):
        serialized = serialized.decode()
    return serialized


def deserialize(serialized, encryption_key=None):
    """
    Deserialized a Python object.

    Args:
        serialized (bytes): serialized Prefect object
        encryption_key (str): key used to decrypt the serialization
    """
    if encryption_key is None:
        encryption_key = prefect.config.get('prefect', 'encryption_key')
    if isinstance(serialized, str):
        serialized = serialized.encode()
    if encryption_key:
        cipher = AESCipher(key=encryption_key)
        serialized = cipher.decrypt(serialized)
    return cloudpickle.loads(base64.b64decode(serialized))
