# pylint: disable=no-member,unused-argument
from __future__ import unicode_literals
import json

from collections import OrderedDict
import six
from django.contrib.auth.models import User
from django.db import transaction
from rest_framework import serializers
from rest_framework import exceptions
from rest_framework.exceptions import PermissionDenied
from vstutils.api import serializers as vst_serializers
from vstutils.api.base import Response

from ...main.models import Inventory
from ...main import models, exceptions as main_exceptions
from ..signals import api_post_save, api_pre_save


# NOTE: we can freely remove that because according to real behaviour all our
#  models always have queryset at this stage, so this code actually doing
# nothing
#
# Serializers field for usability
class ModelRelatedField(serializers.PrimaryKeyRelatedField):
    def __init__(self, **kwargs):
        model = kwargs.pop("model", None)
        assert not ((model is not None or self.queryset is not None) and
                    kwargs.get('read_only', None)), (
            'Relational fields should not provide a `queryset` or `model`'
            ' argument, when setting read_only=`True`.'
        )
        if model is not None:
            kwargs["queryset"] = model.objects.all()
        super(ModelRelatedField, self).__init__(**kwargs)


class DictField(serializers.CharField):

    def to_internal_value(self, data):
        return (
            data
            if (
                isinstance(data, (six.string_types, six.text_type)) or
                isinstance(data, (dict, list))
            )
            else self.fail("Unknown type.")
        )

    def to_representation(self, value):
        return (
            json.loads(value)
            if not isinstance(value, (dict, list))
            else value
        )


def with_signals(func):
    '''
    Decorator for send api_pre_save and api_post_save signals from serializers.
    '''
    def func_wrapper(*args, **kwargs):
        user = args[0].context['request'].user
        with transaction.atomic():
            instance = func(*args, **kwargs)
            api_pre_save.send(
                sender=instance.__class__, instance=instance, user=user
            )
        with transaction.atomic():
            api_post_save.send(
                sender=instance.__class__, instance=instance, user=user
            )
        return instance

    return func_wrapper


# Serializers
class EmptySerializer(serializers.Serializer):
    pass


class _SignalSerializer(serializers.ModelSerializer):
    @with_signals
    def create(self, validated_data):
        return super(_SignalSerializer, self).create(validated_data)

    @with_signals
    def update(self, instance, validated_data):
        return super(_SignalSerializer, self).update(instance, validated_data)

    # Depracated
    # def to_internal_value(self, data):
    #     variables = data.pop("variables", None)
    #     instance = super(_SignalSerializer, self).to_internal_value(data)
    #     if variables:
    #         instance['vars'] = variables
    #     return instance


class _WithPermissionsSerializer(_SignalSerializer):
    perms_msg = "You do not have permission to perform this action."

    def create(self, validated_data):
        validated_data["owner"] = self.current_user()
        return super(_WithPermissionsSerializer, self).create(validated_data)

    def current_user(self):
        return self.context['request'].user

    def __get_all_permission_serializer(self):  # noce
        return PermissionsSerializer(
            self.instance.acl.all(), many=True
        )

    def __duplicates_check(self, data):  # noce
        without_role = [frozenset({e['member'], e['member_type']}) for e in data]
        if len(without_role) != len(list(set(without_role))):
            raise ValueError("There is duplicates in your permissions set.")

    @transaction.atomic
    def __permission_set(self, data, remove_old=True):  # noce
        self.__duplicates_check(data)
        for permission_args in data:
            if remove_old:
                self.instance.acl.all().extend().filter(
                    member=permission_args['member'],
                    member_type=permission_args['member_type']
                ).delete()
            self.instance.acl.create(**permission_args)

    @transaction.atomic
    def permissions(self, request):  # noce
        user = self.current_user()
        if request.method != "GET" and \
                not self.instance.acl_handler.manageable_by(user):
            raise PermissionDenied(self.perms_msg)
        if request.method == "DELETE":
            self.instance.acl.all().filter_by_data(request.data).delete()
        elif request.method == "POST":
            self.__permission_set(request.data)
        elif request.method == "PUT":
            self.instance.acl.clear()
            self.__permission_set(request.data, False)
        return Response(self.__get_all_permission_serializer().data, 200)

    def _change_owner(self, request):  # noce
        if not self.instance.acl_handler.owned_by(self.current_user()):
            raise PermissionDenied(self.perms_msg)
        self.instance.acl_handler.set_owner(User.objects.get(pk=request.data))
        return Response("Owner changed", 200)

    def owner(self, request):  # noce
        if request.method == "GET":
            return Response(self.instance.owner.id, 200)
        elif request.method == "PUT":
            return self._change_owner(request)


class UserSerializer(vst_serializers.UserSerializer):

    @with_signals
    def create(self, data):
        return super(UserSerializer, self).create(data)

    @with_signals
    def update(self, instance, validated_data):
        return super(UserSerializer, self).update(instance, validated_data)


class TeamSerializer(_WithPermissionsSerializer):
    users_list = DictField(required=False, write_only=True)

    class Meta:
        model = models.UserGroup
        fields = (
            'id',
            "name",
            "users_list",
            'url',
        )


class OneTeamSerializer(TeamSerializer):
    users = UserSerializer(many=True, required=False)
    users_list = DictField(required=False)
    owner = UserSerializer(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.UserGroup
        fields = (
            'id',
            "name",
            "notes",
            "users",
            "users_list",
            "owner",
            'url',
        )


class OneUserSerializer(UserSerializer):
    groups = TeamSerializer(read_only=True, many=True)
    raw_password = serializers.HiddenField(default=False, initial=False)
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('id',
                  'username',
                  'password',
                  'raw_password',
                  'is_active',
                  'is_staff',
                  'first_name',
                  'last_name',
                  'email',
                  'groups',
                  'url',)
        read_only_fields = ('is_superuser',
                            'date_joined',)


class HistorySerializer(_SignalSerializer):
    class Meta:
        model = models.History
        fields = ("id",
                  "project",
                  "mode",
                  "kind",
                  "status",
                  "inventory",
                  "start_time",
                  "stop_time",
                  "initiator",
                  "initiator_type",
                  "executor",
                  "revision",
                  "options",
                  "url")


class OneHistorySerializer(_SignalSerializer):
    raw_stdout = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = models.History
        fields = ("id",
                  "project",
                  "mode",
                  "kind",
                  "status",
                  "start_time",
                  "stop_time",
                  "execution_time",
                  "inventory",
                  "raw_inventory",
                  "raw_args",
                  "raw_stdout",
                  "initiator",
                  "initiator_type",
                  "executor",
                  "execute_args",
                  "revision",
                  "options",
                  "url")

    def get_raw(self, request):
        return self.instance.get_raw(request.query_params.get("color", "no") == "yes")

    def get_raw_stdout(self, obj):
        return self.context.get('request').build_absolute_uri("raw/")

    def get_facts(self, request):
        return self.instance.facts


class HistoryLinesSerializer(_SignalSerializer):
    class Meta:
        model = models.HistoryLines
        fields = ("line_number",
                  "line_gnumber",
                  "line",)


class HookSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Hook
        fields = (
            'id',
            'name',
            'type',
            'when',
            'enable',
            'recipients'
        )


class VariableSerializer(_SignalSerializer):
    value = serializers.CharField(default="", allow_blank=True)

    class Meta:
        model = models.Variable
        fields = (
            'id',
            'key',
            'value',
        )

    def to_representation(self, instance):
        result = super(VariableSerializer, self).to_representation(instance)
        if instance.key in getattr(instance.content_object, 'HIDDEN_VARS', []):
            result['value'] = "[~~ENCRYPTED~~]"
        return result


class ProjectVariableSerializer(VariableSerializer):
    project_keys = OrderedDict(
        repo_type='Types of repo. Default="MANUAL".',
        repo_sync_on_run="Sync project by every execution.",
        repo_branch="[Only for GIT repos] Checkout branch on sync.",
        repo_password="[Only for GIT repos] Password to fetch access.",
        repo_key="[Only for GIT repos] Key to fetch access.",
    )
    key = serializers.ChoiceField(choices=tuple(project_keys.items()))


class _WithVariablesSerializer(_WithPermissionsSerializer):
    @transaction.atomic
    def _do_with_vars(self, method_name, *args, **kwargs):
        method = getattr(super(_WithVariablesSerializer, self), method_name)
        instance = method(*args, **kwargs)
        return instance

    def create(self, validated_data):
        return self._do_with_vars("create", validated_data=validated_data)

    def update(self, instance, validated_data):
        children_instance = getattr(instance, "children", False)
        if validated_data.get("children", children_instance) != children_instance:
            raise exceptions.ValidationError("Children not allowed to update.")
        return self._do_with_vars(
            "update", instance, validated_data=validated_data
        )

    def get_vars(self, representation):
        return representation.get('vars', None)

    def to_representation(self, instance, hidden_vars=None):
        rep = super(_WithVariablesSerializer, self).to_representation(instance)
        hv = hidden_vars
        hv = getattr(instance, 'HIDDEN_VARS', []) if hv is None else hv
        vars = self.get_vars(rep)
        if vars is not None:
            for mask_key in hv:
                if mask_key in vars.keys():
                    vars[mask_key] = "[~~ENCRYPTED~~]"
        return rep


class HostSerializer(_WithVariablesSerializer):

    class Meta:
        model = models.Host
        fields = ('id',
                  'name',
                  'type',
                  'url',)


class OneHostSerializer(HostSerializer):
    owner = UserSerializer(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.Host
        fields = ('id',
                  'name',
                  'notes',
                  'type',
                  'owner',
                  'url',)


class PlaybookSerializer(_WithVariablesSerializer):
    class Meta:
        model = models.Task
        fields = ('id',
                  'name',
                  'playbook',)


class OnePlaybookSerializer(PlaybookSerializer):
    playbook = serializers.CharField(read_only=True)

    class Meta:
        model = models.Task
        fields = ('id',
                  'name',
                  'playbook',)


class PeriodictaskSerializer(_WithVariablesSerializer):
    kind = serializers.ChoiceField(
        choices=[(k, k) for k in models.PeriodicTask.kinds],
        required=False,
        default=models.PeriodicTask.kinds[0]
    )
    type = serializers.ChoiceField(
        choices=[(k, k) for k in models.PeriodicTask.types],
        required=False,
        default=models.PeriodicTask.types[0]
    )
    schedule = serializers.CharField(allow_blank=True)
    inventory = serializers.CharField(required=False)
    mode = serializers.CharField(required=False)

    class Meta:
        model = models.PeriodicTask
        fields = ('id',
                  'name',
                  'type',
                  'schedule',
                  'mode',
                  'kind',
                  'inventory',
                  'save_result',
                  'template',
                  'template_opt',
                  'enabled',)

    @transaction.atomic
    def _do_with_vars(self, *args, **kwargs):
        kw = kwargs['validated_data']
        if kw.get('kind', None) == 'TEMPLATE':
            template = kw.get('template')
            kw['project'] = template.project
            kw['inventory'] = ''
            kw['mode'] = ''
            kwargs['validated_data'] = kw
        return super(PeriodictaskSerializer, self)._do_with_vars(*args, **kwargs)

    @transaction.atomic
    def permissions(self, request):  # noce
        raise main_exceptions.NotApplicable("See project permissions.")

    def owner(self, request):  # noce
        raise main_exceptions.NotApplicable("See project owner.")


class OnePeriodictaskSerializer(PeriodictaskSerializer):
    project = ModelRelatedField(required=False, model=models.Project)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.PeriodicTask
        fields = ('id',
                  'name',
                  'notes',
                  'type',
                  'schedule',
                  'mode',
                  'kind',
                  'project',
                  'inventory',
                  'save_result',
                  'template',
                  'template_opt',
                  'enabled',)

    def execute(self):
        inventory = self.instance.inventory
        rdata = dict(
            detail="Started at inventory {}.".format(inventory),
            history_id=self.instance.execute(sync=False)
        )
        return Response(rdata, 201)


class DataSerializer(serializers.Serializer):

    def to_internal_value(self, data):
        return (
            data
            if (
                isinstance(data, (six.string_types, six.text_type)) or
                isinstance(data, (dict, list))
            )
            else self.fail("Unknown type.")
        )

    def to_representation(self, value):
        return (
            json.loads(value)
            if not isinstance(value, (dict, list))
            else value
        )


class TemplateSerializer(_WithVariablesSerializer):
    data = DataSerializer(required=True, write_only=True)
    options = DataSerializer(write_only=True)
    options_list = serializers.ListField(read_only=True)

    class Meta:
        model = models.Template
        fields = (
            'id',
            'name',
            'kind',
            'data',
            'options',
            'options_list',
        )

    def get_vars(self, representation):
        try:
            return representation['data']['vars']
        except KeyError:  # nocv
            return None

    def set_opts_vars(self, rep, hidden_vars):
        if not rep.get('vars', None):
            return rep
        var = rep['vars']
        for mask_key in hidden_vars:
            if mask_key in var.keys():
                var[mask_key] = "[~~ENCRYPTED~~]"
        return rep

    def repr_options(self, instance, data, hidden_vars):
        hv = hidden_vars
        hv = instance.HIDDEN_VARS if hv is None else hv
        for name, rep in data.get('options', {}).items():
            data['options'][name] = self.set_opts_vars(rep, hv)

    def to_representation(self, instance):
        data = OrderedDict()
        if instance.kind in ["Task", "Module"]:
            hidden_vars = models.PeriodicTask.HIDDEN_VARS
            data = super(TemplateSerializer, self).to_representation(
                instance, hidden_vars=hidden_vars
            )
            self.repr_options(instance, data, hidden_vars)
        return data


class OneTemplateSerializer(TemplateSerializer):
    data = DataSerializer(required=True)
    owner = UserSerializer(read_only=True)
    options = DataSerializer(required=False)
    options_list = serializers.ListField(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.Template
        fields = (
            'id',
            'name',
            'notes',
            'kind',
            'owner',
            'data',
            'options',
            'options_list',
        )

    def execute(self, request):
        serializer = OneProjectSerializer(self.instance.project)
        return self.instance.execute(
            serializer, request.user, request.data.get('option', None)
        )


class TemplateExecuteSerializer(serializers.Serializer):
    option = serializers.CharField(
        help_text='Option name from template options.',
        min_length=0, allow_blank=True
    )


###################################
# Subclasses for operations
# with hosts and groups
class _InventoryOperations(_WithVariablesSerializer):
    pass


###################################

class GroupSerializer(_WithVariablesSerializer):

    class Meta:
        model = models.Group
        fields = ('id',
                  'name',
                  'children',
                  'url',)


class OneGroupSerializer(GroupSerializer, _InventoryOperations):
    hosts  = HostSerializer(read_only=True, many=True)
    groups = GroupSerializer(read_only=True, many=True)
    owner = UserSerializer(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.Group
        fields = ('id',
                  'name',
                  'notes',
                  'hosts',
                  "groups",
                  'children',
                  'owner',
                  'url',)

    class ValidationException(exceptions.ValidationError):
        status_code = 409


class InventorySerializer(_WithVariablesSerializer):

    class Meta:
        model = models.Inventory
        fields = ('id',
                  'name',
                  'url',)


class OneInventorySerializer(InventorySerializer, _InventoryOperations):
    all_hosts  = HostSerializer(read_only=True, many=True)
    hosts  = HostSerializer(read_only=True, many=True, source="hosts_list")
    groups = GroupSerializer(read_only=True, many=True, source="groups_list")
    owner = UserSerializer(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.Inventory
        fields = ('id',
                  'name',
                  'notes',
                  'hosts',
                  'all_hosts',
                  "groups",
                  'owner',
                  'url',)


class ProjectSerializer(_InventoryOperations):
    status = serializers.CharField(read_only=True)
    type   = serializers.CharField(read_only=True)

    class Meta:
        model = models.Project
        fields = ('id',
                  'name',
                  'status',
                  'type',
                  'url',)

    @transaction.atomic
    def _do_with_vars(self, *args, **kw):
        instance = super(ProjectSerializer, self)._do_with_vars(*args, **kw)
        return instance if instance.repo_class else None


class OneProjectSerializer(ProjectSerializer, _InventoryOperations):
    repository  = serializers.CharField(default='MANUAL')
    hosts       = HostSerializer(read_only=True, many=True)
    groups      = GroupSerializer(read_only=True, many=True)
    inventories = InventorySerializer(read_only=True, many=True)
    owner = UserSerializer(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = models.Project
        fields = ('id',
                  'name',
                  'notes',
                  'status',
                  'repository',
                  'hosts',
                  "groups",
                  'inventories',
                  'owner',
                  'revision',
                  'branch',
                  'readme_content',
                  'readme_ext',
                  'url',)

    @transaction.atomic()
    def sync(self):
        self.instance.start_repo_task("sync")
        data = dict(detail="Sync with {}.".format(self.instance.repository))
        return Response(data, 200)

    def _execution(self, kind, data, user, **kwargs):
        template = kwargs.pop("template", None)
        inventory = data.pop("inventory")
        try:
            inventory = Inventory.objects.get(id=int(inventory))
            if not inventory.acl_handler.viewable_by(user):  # nocv
                raise PermissionDenied(
                    "You don't have permission to inventory."
                )
        except ValueError:
            pass
        if template is not None:
            init_type = "template"
            obj_id = template
            data['template_option'] = kwargs.get('template_option', None)
        else:
            init_type = "project"
            obj_id = self.instance.id
        history_id = self.instance.execute(
            kind, str(data.pop(kind)), inventory,
            initiator=obj_id, initiator_type=init_type, executor=user, **data
        )
        rdata = dict(
            detail="Started at inventory {}.".format(inventory),
            history_id=history_id, executor=user.id
        )
        return Response(rdata, 201)

    def execute_playbook(self, request):
        return self._execution("playbook", dict(request.data), request.user)

    def execute_module(self, request):
        return self._execution("module", dict(request.data), request.user)


class PermissionsSerializer(_SignalSerializer):
    member = serializers.IntegerField()
    member_type = serializers.ChoiceField(choices=(
        ('user', 'user'),
        ('team', 'team'),
    ))
    role = serializers.ChoiceField(choices=(
        ('MASTER', 'Full controlled'),
        ('EDITOR', 'Write and edit'),
        ('EXECUTOR', 'Read and execute'),
    ))

    class Meta:
        model = models.ACLPermission
        fields = ("member",
                  "role",
                  "member_type")
