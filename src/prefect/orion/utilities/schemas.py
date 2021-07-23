import json
from typing import List
from pydantic import BaseModel, create_model


def subclass_model(
    base: BaseModel,
    name: str = None,
    include: List[str] = None,
    exclude: List[str] = None,
) -> BaseModel:
    """Creates a subclass of a Pydantic model that excludes certain fields.
    Pydantic models use the __fields__ attribute of their parent class to
    determine inherited fields, so to create a subclass without fields, we
    temporarily remove those fields from the parent __fields__ and use
    `create_model` to dynamically generate a new subclass.

    Args:
        cls (pydantic.BaseModel): a Pydantic BaseModel
        include (List[str]): a set of field names to include. If `None`, all fields are included.
        exclude (List[str]): a list of field names to exclude. If `None`, no fields are excluded.

    Returns:
        pydantic.BaseModel: a new model subclass that contains only the specified fields.

    Example:
        class Parent(pydantic.BaseModel):
            x: int = 1
            y: int = 2

        Child = subclass_model(Parent, 'Child', exclude=['y'])

        # equivalent, for extending the subclass further
        # with new fields
        class Child(subclass_model(Parent, exclude=['y'])):
            pass

        assert hasattr(Child(), 'x')
        assert not hasattr(Child(), 'y')
    """
    # copying a class doesn't work (`base is deepcopy(base)`), so we need to
    # make sure we don't modify the actual parent class. Instead, we store its
    # original __fields__ attribute, replace it with a modified one for the
    # subclass operation, and then restore the original value.
    original_fields = base.__fields__

    # collect required field names
    field_names = set(include or base.__fields__)
    field_names.difference_update(exclude or [])

    # create model
    base.__fields__ = {k: v for k, v in base.__fields__.items() if k in field_names}
    new_cls = create_model(name or base.__name__, __base__=base)

    # restore original __fields__
    base.__fields__ = original_fields

    return new_cls


class PrefectBaseModel(BaseModel):
    class Config:
        extra = "forbid"

    def json_dict(self, *args, **kwargs) -> dict:
        """Returns a dict of JSON-compatible values, equivalent
        to `json.loads(self.json())`.

        `self.dict()` returns Python-native types, including UUIDs
        and datetimes; `self.json()` returns a JSON string. This
        method is useful when we require a JSON-compatible Python
        object.

        Returns:
            dict: a JSON-compatible dict
        """
        return json.loads(self.json(*args, **kwargs))
