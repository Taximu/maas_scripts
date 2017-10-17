# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "AnonNodesHandler",
    "NodeHandler",
    "NodesHandler",
    "store_node_power_parameters",
    ]

from base64 import b64decode

import os
import bson
import crochet
from django.conf import settings
from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
    )
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from maasserver import locks
from maasserver.api.logger import maaslog
from maasserver.api.support import (
    admin_method,
    AnonymousOperationsHandler,
    operation,
    OperationsHandler,
    )
from maasserver.api.utils import (
    get_mandatory_param,
    get_oauth_token,
    get_optional_list,
    get_optional_param,
    )
from maasserver.clusterrpc.power_parameters import get_power_types
from maasserver.dns.config import change_dns_zones
from maasserver.enum import (
    IPADDRESS_TYPE,
    NODE_PERMISSION,
    NODE_STATUS,
    )
from maasserver.exceptions import (
    MAASAPIBadRequest,
    NodesNotAvailable,
    NodeStateViolation,
    PowerProblem,
    StaticIPAddressExhaustion,
    Unauthorized,
    )
from maasserver.fields import MAC_RE
from maasserver.forms import (
    BulkNodeActionForm,
    get_action_form,
    get_node_create_form,
    get_node_edit_form,
    NodeActionForm,
    )
from maasserver.models import (
    MACAddress,
    Node,
    )
from maasserver.models.node import RELEASABLE_STATUSES
from maasserver.models.nodeprobeddetails import get_single_probed_details
from maasserver.node_action import Commission
from maasserver.node_constraint_filter_forms import AcquireNodeForm
from maasserver.rpc import getClientFor
from maasserver.utils import find_nodegroup
from maasserver.utils.orm import get_first
from piston.utils import rc
from provisioningserver.power.poweraction import (
    PowerActionFail,
    UnknownPowerType,
    )
from provisioningserver.power_schema import UNKNOWN_POWER_TYPE
from provisioningserver.rpc.cluster import PowerQuery
from provisioningserver.rpc.exceptions import NoConnectionsAvailable
import simplejson as json

# Node's fields exposed on the API.
DISPLAYED_NODE_FIELDS = (
    'system_id',
    'hostname',
    'owner',
    ('macaddress_set', ('mac_address',)),
    'architecture',
    'cpu_count',
    'memory',
    'storage',
    'status',
    'substatus',
    'osystem',
    'distro_series',
    'boot_type',
    'netboot',
    'power_type',
    'power_state',
    'tag_names',
    'ip_addresses',
    'routers',
    'zone',
    'disable_ipv4',
    )


def store_node_power_parameters(node, request):
    """Store power parameters in request.

    The parameters should be JSON, passed with key `power_parameters`.
    """
    power_type = request.POST.get("power_type", None)
    if power_type is None:
        return

    power_types = get_power_types([node.nodegroup])

    if power_type in power_types or power_type == UNKNOWN_POWER_TYPE:
        node.power_type = power_type
    else:
        raise MAASAPIBadRequest("Bad power_type '%s'" % power_type)

    power_parameters = request.POST.get("power_parameters", None)
    if power_parameters and not power_parameters.isspace():
        try:
            node.power_parameters = json.loads(power_parameters)
        except ValueError:
            raise MAASAPIBadRequest("Failed to parse JSON power_parameters")

    node.save()


class NodeHandler(OperationsHandler):
    """Manage an individual Node.

    The Node is identified by its system_id.
    """
    api_doc_section_name = "Node"

    create = None  # Disable create.
    model = Node
    fields = DISPLAYED_NODE_FIELDS

    @classmethod
    def status(handler, node):
        """Backward-compatibility layer: fold deployment-related statuses.

        Before the lifecycle of a node got reworked, 'allocated' meant a lot
        of things (allocated, deploying and deployed).  This is a backward
        compatiblity layer so that clients relying on the old behavior won't
        break.
        """
        old_allocated_status_aliases = [
            NODE_STATUS.ALLOCATED, NODE_STATUS.DEPLOYING,
            NODE_STATUS.DEPLOYED, NODE_STATUS.FAILED_DEPLOYMENT]
        old_deployed_status_aliases = [
            NODE_STATUS.RELEASING, NODE_STATUS.DISK_ERASING,
            NODE_STATUS.FAILED_RELEASING, NODE_STATUS.FAILED_DISK_ERASING,
            ]
        deployed_aliases = (
            old_allocated_status_aliases + old_deployed_status_aliases)
        if node.status in deployed_aliases:
            return 6  # Old allocated status.
        else:
            return node.status

    @classmethod
    def substatus(handler, node):
        """Return the substatus of the node.

        The node's status as exposed on the API corresponds to a subset of the
        actual possible statuses.  This was done to preserve backward
        compatiblity between MAAS releases.  This 'substatus' field exposes
        all the node's possible statuses as designed after the lifecyle of a
        node got reworked.
        """
        return node.status

    # Override the 'hostname' field so that it returns the FQDN instead as
    # this is used by Juju to reach that node.
    @classmethod
    def hostname(handler, node):
        return node.fqdn

    # Override 'owner' so it emits the owner's name rather than a
    # full nested user object.
    @classmethod
    def owner(handler, node):
        if node.owner is None:
            return None
        return node.owner.username

    def read(self, request, system_id):
        """Read a specific Node.

        Returns 404 if the node is not found.
        """
        return Node.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.VIEW)

    def update(self, request, system_id):
        """Update a specific Node.

        :param hostname: The new hostname for this node.
        :type hostname: unicode
        :param architecture: The new architecture for this node.
        :type architecture: unicode
        :param power_type: The new power type for this node. If you use the
            default value, power_parameters will be set to the empty string.
            Available to admin users.
            See the `Power types`_ section for a list of the available power
            types.
        :type power_type: unicode
        :param power_parameters_{param1}: The new value for the 'param1'
            power parameter.  Note that this is dynamic as the available
            parameters depend on the selected value of the Node's power_type.
            For instance, if the power_type is 'ether_wake', the only valid
            parameter is 'power_address' so one would want to pass 'myaddress'
            as the value of the 'power_parameters_power_address' parameter.
            Available to admin users.
            See the `Power types`_ section for a list of the available power
            parameters for each power type.
        :type power_parameters_{param1}: unicode
        :param power_parameters_skip_check: Whether or not the new power
            parameters for this node should be checked against the expected
            power parameters for the node's power type ('true' or 'false').
            The default is 'false'.
        :type power_parameters_skip_check: unicode
        :param zone: Name of a valid physical zone in which to place this node
        :type zone: unicode
        :param boot_type: The installation type of the node. 'fastpath': use
            the default installer. 'di' use the debian installer.
            Note that using 'di' is now deprecated and will be removed in favor
            of the default installer in MAAS 1.9.
        :type boot_type: unicode

        Returns 404 if the node is node found.
        Returns 403 if the user does not have permission to update the node.
        """
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)
        Form = get_node_edit_form(request.user)
        form = Form(data=request.data, instance=node)
        if form.is_valid():
            return form.save()
        else:
            raise ValidationError(form.errors)

    def delete(self, request, system_id):
        """Delete a specific Node.

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to delete the node.
        Returns 204 if the node is successfully deleted.
        """
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.ADMIN)
        node.delete()
        return rc.DELETED

    @classmethod
    def resource_uri(cls, node=None):
        # This method is called by piston in two different contexts:
        # - when generating an uri template to be used in the documentation
        # (in this case, it is called with node=None).
        # - when populating the 'resource_uri' field of an object
        # returned by the API (in this case, node is a Node object).
        node_system_id = "system_id"
        if node is not None:
            node_system_id = node.system_id
        return ('node_handler', (node_system_id, ))

    @operation(idempotent=False)
    def stop(self, request, system_id):
        """Shut down a node.

        :param stop_mode: An optional power off mode. If 'soft',
            perform a soft power down if the node's power type supports
            it, otherwise perform a hard power off. For all values other
            than 'soft', and by default, perform a hard power off. A
            soft power off generally asks the OS to shutdown the system
            gracefully before powering off, while a hard power off
            occurs immediately without any warning to the OS.
        :type stop_mode: unicode

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to stop the node.
        """
        stop_mode = request.POST.get('stop_mode', 'hard')
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.EDIT)
	cmd = 'python /maas_extra/shutdown_manually.py ' + node.hostname
        os.system(cmd)
        power_action_sent = node.stop(request.user, stop_mode=stop_mode)
        if power_action_sent:
            return node
        else:
            return node

    @operation(idempotent=False)
    def start(self, request, system_id):
        """Power up a node.

        :param user_data: If present, this blob of user-data to be made
            available to the nodes through the metadata service.
        :type user_data: base64-encoded unicode
        :param distro_series: If present, this parameter specifies the
            OS release the node will use.
        :type distro_series: unicode

        Ideally we'd have MIME multipart and content-transfer-encoding etc.
        deal with the encapsulation of binary data, but couldn't make it work
        with the framework in reasonable time so went for a dumb, manual
        encoding instead.

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to stop the node.
        Returns 503 if the start-up attempted to allocate an IP address,
        and there were no IP addresses available on the relevant cluster
        interface.
        """
        user_data = request.POST.get('user_data', None)
        series = request.POST.get('distro_series', None)
        license_key = request.POST.get('license_key', None)

        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.EDIT)

        if user_data is not None:
            user_data = b64decode(user_data)
        if series is not None or license_key is not None:
            Form = get_node_edit_form(request.user)
            form = Form(instance=node)
            if series is not None:
                form.set_distro_series(series=series)
            if license_key is not None:
                form.set_license_key(license_key=license_key)
            if form.is_valid():
                form.save()
            else:
                raise ValidationError(form.errors)

        try:
            node.start(request.user, user_data=user_data)
        except StaticIPAddressExhaustion:
            # The API response should contain error text with the
            # system_id in it, as that is the primary API key to a node.
            raise StaticIPAddressExhaustion(
                "%s: Unable to allocate static IP due to address"
                " exhaustion." % system_id)
        return node

    @operation(idempotent=False)
    def release(self, request, system_id):
        """Release a node.  Opposite of `NodesHandler.acquire`.

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to release the node.
        Returns 409 if the node is in a state where it may not be released.
        """
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.EDIT)
        if node.status == NODE_STATUS.READY:
            # Nothing to do.  This may be a redundant retry, and the
            # postcondition is achieved, so call this success.
            pass
        elif node.status in RELEASABLE_STATUSES:
            node.release_or_erase()
        else:
            raise NodeStateViolation(
                "Node cannot be released in its current state ('%s')."
                % node.display_status())
        return node

    @operation(idempotent=False)
    def commission(self, request, system_id):
        """Begin commissioning process for a node.

        A node in the 'ready', 'declared' or 'failed test' state may
        initiate a commissioning cycle where it is checked out and tested
        in preparation for transitioning to the 'ready' state. If it is
        already in the 'ready' state this is considered a re-commissioning
        process which is useful if commissioning tests were changed after
        it previously commissioned.

        Returns 404 if the node is not found.
        """
        node = get_object_or_404(Node, system_id=system_id)
        form_class = get_action_form(user=request.user)
        form = form_class(
            node, data={NodeActionForm.input_name: Commission.name})
        if form.is_valid():
            node = form.save(allow_redirect=False)
            return node
        else:
            raise ValidationError(form.errors)

    @operation(idempotent=True)
    def details(self, request, system_id):
        """Obtain various system details.

        For example, LLDP and ``lshw`` XML dumps.

        Returns a ``{detail_type: xml, ...}`` map, where
        ``detail_type`` is something like "lldp" or "lshw".

        Note that this is returned as BSON and not JSON. This is for
        efficiency, but mainly because JSON can't do binary content
        without applying additional encoding like base-64.

        Returns 404 if the node is not found.
        """
        node = get_object_or_404(Node, system_id=system_id)
        probe_details = get_single_probed_details(node.system_id)
        probe_details_report = {
            name: None if data is None else bson.Binary(data)
            for name, data in probe_details.items()
        }
        return HttpResponse(
            bson.BSON.encode(probe_details_report),
            # Not sure what media type to use here.
            content_type='application/bson')

    @admin_method
    @operation(idempotent=False)
    def claim_sticky_ip_address(self, request, system_id):
        """Assign a "sticky" IP address to a Node's MAC.

        This method is reserved for admin users.

        :param mac_address: Optional MAC address on the node on which to
            assign the sticky IP address.  If not passed, defaults to the
            primary MAC for the node.
        :param requested_address: Optional IP address to claim.  Must be in
            the range defined on a cluster interface to which the context
            MAC is related, or 403 Forbidden is returned.  If the requested
            address is unavailable for use, 404 Not Found is returned.

        A sticky IP is one which stays with the node until the IP is
        disassociated with the node, or the node is deleted.  It allows
        an admin to give a node a stable IP, since normally an automatic
        IP is allocated to a node only during the time a user has
        acquired and started a node.

        Returns 404 if the node is not found.
        Returns 409 if the node is in an allocated state.
        Returns 400 if the mac_address is not found on the node.
        Returns 503 if there are not enough IPs left on the cluster interface
        to which the mac_address is linked.
        """
        node = get_object_or_404(Node, system_id=system_id)
        if node.status == NODE_STATUS.ALLOCATED:
            raise NodeStateViolation(
                "Sticky IP cannot be assigned to a node that is allocated")

        raw_mac = request.POST.get('mac_address', None)
        if raw_mac is None:
            mac_address = node.get_primary_mac()
        else:
            try:
                mac_address = MACAddress.objects.get(
                    mac_address=raw_mac, node=node)
            except MACAddress.DoesNotExist:
                raise MAASAPIBadRequest(
                    "mac_address %s not found on the node" % raw_mac)
        requested_address = request.POST.get('requested_address', None)
        sticky_ips = mac_address.claim_static_ips(
            alloc_type=IPADDRESS_TYPE.STICKY,
            requested_address=requested_address)
        claims = [
            (static_ip.ip, mac_address.mac_address.get_raw())
            for static_ip in sticky_ips]
        node.update_host_maps(claims)
        change_dns_zones(node.nodegroup)
        maaslog.info(
            "%s: Sticky IP address(es) allocated: %s", node.hostname,
            ', '.join(allocation.ip for allocation in sticky_ips))
        return node

    @operation(idempotent=False)
    def mark_broken(self, request, system_id):
        """Mark a node as 'broken'.

        If the node is allocated, release it first.

        :param error_description: An optional description of the reason the
            node is being marked broken.
        :type error_description: unicode

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to mark the node
        broken.
        """
        node = Node.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.EDIT)
        error_description = get_optional_param(
            request.POST, 'error_description', '')
        node.mark_broken(error_description)
        return node

    @operation(idempotent=False)
    def mark_fixed(self, request, system_id):
        """Mark a broken node as fixed and set its status as 'ready'.

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to mark the node
        broken.
        """
        node = Node.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.ADMIN)
        node.mark_fixed()
        maaslog.info(
            "%s: User %s marked node as fixed", node.hostname,
            request.user.username)
        return node

    @admin_method
    @operation(idempotent=True)
    def power_parameters(self, request, system_id):
        """Obtain power parameters.

        This method is reserved for admin users and returns a 403 if the
        user is not one.

        This returns the power parameters, if any, configured for a
        node. For some types of power control this will include private
        information such as passwords and secret keys.

        Returns 404 if the node is not found.
        """
        node = get_object_or_404(Node, system_id=system_id)
        return node.power_parameters

    @operation(idempotent=True)
    def query_power_state(self, request, system_id):
        """Query the power state of a node.

        Send a request to the node's power controller which asks it about
        the node's state.  The reply to this could be delayed by up to
        30 seconds while waiting for the power controller to respond.
        Use this method sparingly as it ties up an appserver thread
        while waiting.

        :param system_id: The node to query.
        :return: a dict whose key is "state" with a value of one of
            'on' or 'off'.

        Returns 404 if the node is not found.
        Returns 503 (with explanatory text) if the power state could not
        be queried.
        """
        node = get_object_or_404(Node, system_id=system_id)
        ng = node.nodegroup

        try:
            client = getClientFor(ng.uuid)
        except NoConnectionsAvailable:
            maaslog.error(
                "Unable to get RPC connection for cluster '%s' (%s)",
                ng.cluster_name, ng.uuid)
            raise PowerProblem("Unable to connect to cluster controller")

        try:
            power_info = node.get_effective_power_info()
        except UnknownPowerType as e:
            raise PowerProblem(e)
        if not power_info.can_be_started:
            raise PowerProblem("Power state is not queryable")

        call = client(
            PowerQuery, system_id=system_id, hostname=node.hostname,
            power_type=power_info.power_type,
            context=power_info.power_parameters)
        try:
            # Allow 30 seconds for the power query max as we're holding
            # up an appserver thread here.
            state = call.wait(30)
        except crochet.TimeoutError:
            maaslog.error(
                "%s: Timed out waiting for power response in Node.power_state",
                node.hostname)
            raise PowerProblem("Timed out waiting for power response")
        except (NotImplementedError, PowerActionFail) as e:
            raise PowerProblem(e)

        return state

    @operation(idempotent=False)
    def abort_operation(self, request, system_id):
        """Abort a node's current operation.

        This currently only supports aborting of the 'Disk Erasing' operation.

        Returns 404 if the node could not be found.
        Returns 403 if the user does not have permission to abort the
        current operation.
        """
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.EDIT)
        node.abort_operation(request.user)
        return node


def create_node(request):
    """Service an http request to create a node.

    The node will be in the New state.

    :param request: The http request for this node to be created.
    :return: A `Node`.
    :rtype: :class:`maasserver.models.Node`.
    :raises: ValidationError
    """

    # For backwards compatibilty reasons, requests may be sent with:
    #     architecture with a '/' in it: use normally
    #     architecture without a '/' and no subarchitecture: assume 'generic'
    #     architecture without a '/' and a subarchitecture: use as specified
    #     architecture with a '/' and a subarchitecture: error
    given_arch = request.data.get('architecture', None)
    given_subarch = request.data.get('subarchitecture', None)
    altered_query_data = request.data.copy()
    if given_arch and '/' in given_arch:
        if given_subarch:
            # Architecture with a '/' and a subarchitecture: error.
            raise ValidationError('Subarchitecture cannot be specified twice.')
        # Architecture with a '/' in it: use normally.
    elif given_arch:
        if given_subarch:
            # Architecture without a '/' and a subarchitecture:
            # use as specified.
            altered_query_data['architecture'] = '/'.join(
                [given_arch, given_subarch])
            del altered_query_data['subarchitecture']
        else:
            # Architecture without a '/' and no subarchitecture:
            # assume 'generic'.
            altered_query_data['architecture'] += '/generic'

    if 'nodegroup' not in altered_query_data:
        # If 'nodegroup' is not explicitely specified, get the origin of the
        # request to figure out which nodegroup the new node should be
        # attached to.
        if request.data.get('autodetect_nodegroup', None) is None:
            # We insist on this to protect command-line API users who
            # are manually enlisting nodes.  You can't use the origin's
            # IP address to indicate in which nodegroup the new node belongs.
            raise ValidationError(
                "'autodetect_nodegroup' must be specified if 'nodegroup' "
                "parameter missing")
        nodegroup = find_nodegroup(request)
        if nodegroup is not None:
            altered_query_data['nodegroup'] = nodegroup

    Form = get_node_create_form(request.user)
    form = Form(data=altered_query_data)
    if form.is_valid():
        node = form.save()
        # Hack in the power parameters here.
        store_node_power_parameters(node, request)
        maaslog.info("%s: Enlisted new node", node.hostname)
        return node
    else:
        raise ValidationError(form.errors)


class AnonNodesHandler(AnonymousOperationsHandler):
    """Anonymous access to Nodes."""
    create = read = update = delete = None
    model = Node
    fields = DISPLAYED_NODE_FIELDS

    # Override the 'hostname' field so that it returns the FQDN instead as
    # this is used by Juju to reach that node.
    @classmethod
    def hostname(handler, node):
        return node.fqdn

    @operation(idempotent=False)
    def new(self, request):
        """Create a new Node.

        Adding a server to a MAAS puts it on a path that will wipe its disks
        and re-install its operating system.  In anonymous enlistment and when
        the enlistment is done by a non-admin, the node is held in the
        "New" state for approval by a MAAS admin.
        :param boot_type: The installation type of the node. 'fastpath': use
            the default installer. 'di' use the debian installer.
            Note that using 'di' is now deprecated and will be removed in favor
            of the default installer in MAAS 1.9.
        :type boot_type: unicode
        """
        # XXX 2014-02-11 bug=1278685
        # There's no documentation here on what parameters can be passed!

        # Note that request.autodetect_nodegroup is treated as a
        # boolean; its presence indicates True.
        return create_node(request)

    @operation(idempotent=True)
    def is_registered(self, request):
        """Returns whether or not the given MAC address is registered within
        this MAAS (and attached to a non-retired node).

        :param mac_address: The mac address to be checked.
        :type mac_address: unicode
        :return: 'true' or 'false'.
        :rtype: unicode

        Returns 400 if any mandatory parameters are missing.
        """
        mac_address = get_mandatory_param(request.GET, 'mac_address')
        mac_addresses = MACAddress.objects.filter(mac_address=mac_address)
        mac_addresses = mac_addresses.exclude(node__status=NODE_STATUS.RETIRED)
        return mac_addresses.exists()

    @operation(idempotent=False)
    def accept(self, request):
        """Accept a node's enlistment: not allowed to anonymous users.

        Always returns 401.
        """
        raise Unauthorized("You must be logged in to accept nodes.")

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('nodes_handler', [])


class NodesHandler(OperationsHandler):
    """Manage the collection of all the nodes in the MAAS."""
    api_doc_section_name = "Nodes"
    create = read = update = delete = None
    anonymous = AnonNodesHandler

    @operation(idempotent=False)
    def new(self, request):
        """Create a new Node.

        When a node has been added to MAAS by an admin MAAS user, it is
        ready for allocation to services running on the MAAS.
        The minimum data required is:
        architecture=<arch string> (e.g. "i386/generic")
        mac_addresses=<value> (e.g. "aa:bb:cc:dd:ee:ff")

        :param architecture: A string containing the architecture type of
            the node.
        :param mac_addresses: One or more MAC addresses for the node.
        :param hostname: A hostname. If not given, one will be generated.
        :param power_type: A power management type, if applicable (e.g.
            "virsh", "ipmi").
        """
        node = create_node(request)
        if request.user.is_superuser:
            node.accept_enlistment(request.user)
        return node

    def _check_system_ids_exist(self, system_ids):
        """Check that the requested system_ids actually exist in the DB.

        We don't check if the current user has rights to do anything with them
        yet, just that the strings are valid. If not valid raise a BadRequest
        error.
        """
        if not system_ids:
            return
        existing_nodes = Node.objects.filter(system_id__in=system_ids)
        existing_ids = set(existing_nodes.values_list('system_id', flat=True))
        unknown_ids = system_ids - existing_ids
        if len(unknown_ids) > 0:
            raise MAASAPIBadRequest(
                "Unknown node(s): %s." % ', '.join(unknown_ids))

    @operation(idempotent=False)
    def accept(self, request):
        """Accept declared nodes into the MAAS.

        Nodes can be enlisted in the MAAS anonymously or by non-admin users,
        as opposed to by an admin.  These nodes are held in the New
        state; a MAAS admin must first verify the authenticity of these
        enlistments, and accept them.

        Enlistments can be accepted en masse, by passing multiple nodes to
        this call.  Accepting an already accepted node is not an error, but
        accepting one that is already allocated, broken, etc. is.

        :param nodes: system_ids of the nodes whose enlistment is to be
            accepted.  (An empty list is acceptable).
        :return: The system_ids of any nodes that have their status changed
            by this call.  Thus, nodes that were already accepted are
            excluded from the result.

        Returns 400 if any of the nodes do not exist.
        Returns 403 if the user is not an admin.
        """
        system_ids = set(request.POST.getlist('nodes'))
        # Check the existence of these nodes first.
        self._check_system_ids_exist(system_ids)
        # Make sure that the user has the required permission.
        nodes = Node.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.ADMIN, ids=system_ids)
        if len(nodes) < len(system_ids):
            permitted_ids = set(node.system_id for node in nodes)
            raise PermissionDenied(
                "You don't have the required permission to accept the "
                "following node(s): %s." % (
                    ', '.join(system_ids - permitted_ids)))
        return filter(
            None, [node.accept_enlistment(request.user) for node in nodes])

    @operation(idempotent=False)
    def accept_all(self, request):
        """Accept all declared nodes into the MAAS.

        Nodes can be enlisted in the MAAS anonymously or by non-admin users,
        as opposed to by an admin.  These nodes are held in the New
        state; a MAAS admin must first verify the authenticity of these
        enlistments, and accept them.

        :return: Representations of any nodes that have their status changed
            by this call.  Thus, nodes that were already accepted are excluded
            from the result.
        """
        nodes = Node.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.ADMIN)
        nodes = nodes.filter(status=NODE_STATUS.NEW)
        nodes = [node.accept_enlistment(request.user) for node in nodes]
        return filter(None, nodes)

    @operation(idempotent=False)
    def check_commissioning(self, request):
        """Check all commissioning nodes to see if they are taking too long.

        Anything that has been commissioning for longer than
        settings.COMMISSIONING_TIMEOUT is moved into the
        FAILED_COMMISSIONING status.
        """
        # Compute the cutoff time on the database, using the database's
        # clock to compare to the "updated" timestamp, also set from the
        # database's clock.  Otherwise, a sufficient difference between the
        # two clocks (including timezone offset!) might cause commissioning to
        # "time out" immediately, or hours late.
        #
        # This timeout relies on nothing else updating the commissioning node
        # within the hour.  Otherwise, the timestamp will be refreshed as a
        # side effect and timeout will be postponed.
        #
        # This query both identifies and updates the failed nodes.  It
        # refreshes the "updated" timestamp, but does not run any Django-side
        # code associated with saving the nodes.
        params = {
            'commissioning': NODE_STATUS.COMMISSIONING,
            'failed_tests': NODE_STATUS.FAILED_COMMISSIONING,
            'minutes': settings.COMMISSIONING_TIMEOUT
            }
        query = Node.objects.raw("""
            UPDATE maasserver_node
            SET
                status = %(failed_tests)s,
                updated = now()
            WHERE
                status = %(commissioning)s AND
                updated <= (now() - interval '%(minutes)f minutes')
            RETURNING *
            """ % params)
        results = list(query)
        # Note that Django doesn't call save() on updated nodes here,
        # but I don't think anything requires its effects anyway.
        return results

    @operation(idempotent=False)
    def release(self, request):
        """Release multiple nodes.

        This places the nodes back into the pool, ready to be reallocated.

        :param nodes: system_ids of the nodes which are to be released.
           (An empty list is acceptable).
        :return: The system_ids of any nodes that have their status
            changed by this call. Thus, nodes that were already released
            are excluded from the result.

        Returns 400 if any of the nodes cannot be found.
        Returns 403 if the user does not have permission to release any of
        the nodes.
        Returns a 409 if any of the nodes could not be released due to their
        current state.
        """
        system_ids = set(request.POST.getlist('nodes'))
         # Check the existence of these nodes first.
        self._check_system_ids_exist(system_ids)
        # Make sure that the user has the required permission.
        nodes = Node.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.EDIT, ids=system_ids)
        if len(nodes) < len(system_ids):
            permitted_ids = set(node.system_id for node in nodes)
            raise PermissionDenied(
                "You don't have the required permission to release the "
                "following node(s): %s." % (
                    ', '.join(system_ids - permitted_ids)))

        released_ids = []
        failed = []
        for node in nodes:
            if node.status == NODE_STATUS.READY:
                # Nothing to do.
                pass
            elif node.status in RELEASABLE_STATUSES:
                node.release_or_erase()
                released_ids.append(node.system_id)
            else:
                failed.append(
                    "%s ('%s')"
                    % (node.system_id, node.display_status()))

        if any(failed):
            raise NodeStateViolation(
                "Node(s) cannot be released in their current state: %s."
                % ', '.join(failed))
        return released_ids

    @operation(idempotent=True)
    def list(self, request):
        """List Nodes visible to the user, optionally filtered by criteria.

        :param hostname: An optional list of hostnames.  Only nodes with
            matching hostnames will be returned.
        :type hostname: iterable
        :param mac_address: An optional list of MAC addresses.  Only
            nodes with matching MAC addresses will be returned.
        :type mac_address: iterable
        :param id: An optional list of system ids.  Only nodes with
            matching system ids will be returned.
        :type id: iterable
        :param zone: An optional name for a physical zone. Only nodes in the
            zone will be returned.
        :type zone: unicode
        :param agent_name: An optional agent name.  Only nodes with
            matching agent names will be returned.
        :type agent_name: unicode
        """
        # Get filters from request.
        match_ids = get_optional_list(request.GET, 'id')
        match_macs = get_optional_list(request.GET, 'mac_address')
        if match_macs is not None:
            invalid_macs = [
                mac for mac in match_macs if MAC_RE.match(mac) is None]
            if len(invalid_macs) != 0:
                raise ValidationError(
                    "Invalid MAC address(es): %s" % ", ".join(invalid_macs))

        # Fetch nodes and apply filters.
        nodes = Node.objects.get_nodes(
            request.user, NODE_PERMISSION.VIEW, ids=match_ids)
        if match_macs is not None:
            nodes = nodes.filter(macaddress__mac_address__in=match_macs)
        match_hostnames = get_optional_list(request.GET, 'hostname')
        if match_hostnames is not None:
            nodes = nodes.filter(hostname__in=match_hostnames)
        match_zone_name = request.GET.get('zone', None)
        if match_zone_name is not None:
            nodes = nodes.filter(zone__name=match_zone_name)
        match_agent_name = request.GET.get('agent_name', None)
        if match_agent_name is not None:
            nodes = nodes.filter(agent_name=match_agent_name)

        # Prefetch related objects that are needed for rendering the result.
        nodes = nodes.prefetch_related('macaddress_set__node')
        nodes = nodes.prefetch_related('macaddress_set__ip_addresses')
        nodes = nodes.prefetch_related('tags')
        nodes = nodes.select_related('nodegroup')
        nodes = nodes.prefetch_related('nodegroup__dhcplease_set')
        nodes = nodes.prefetch_related('nodegroup__nodegroupinterface_set')
        nodes = nodes.prefetch_related('zone')
        return nodes.order_by('id')

    @operation(idempotent=True)
    def list_allocated(self, request):
        """Fetch Nodes that were allocated to the User/oauth token."""
        token = get_oauth_token(request)
        match_ids = get_optional_list(request.GET, 'id')
        nodes = Node.objects.get_allocated_visible_nodes(token, match_ids)
        return nodes.order_by('id')

    @operation(idempotent=False)
    def acquire(self, request):
        """Acquire an available node for deployment.

        Constraints parameters can be used to acquire a node that possesses
        certain characteristics.  All the constraints are optional and when
        multiple constraints are provided, they are combined using 'AND'
        semantics.

        :param name: Hostname of the returned node.
        :type name: unicode
        :param arch: Architecture of the returned node (e.g. 'i386/generic',
            'amd64', 'armhf/highbank', etc.).
        :type arch: unicode
        :param cpu_count: The minium number of CPUs the returned node must
            have.
        :type cpu_count: int
        :param mem: The minimum amount of memory (expressed in MB) the
             returned node must have.
        :type mem: float
        :param tags: List of tags the returned node must have.
        :type tags: list of unicodes
        :param not_tags: List of tags the acquired node must not have.
        :type tags: List of unicodes.
        :param connected_to: List of routers' MAC addresses the returned
            node must be connected to.
        :type connected_to: unicode or list of unicodes
        :param networks: List of networks (defined in MAAS) to which the node
            must be attached.  A network can be identified by the name
            assigned to it in MAAS; or by an `ip:` prefix followed by any IP
            address that falls within the network; or a `vlan:` prefix
            followed by a numeric VLAN tag, e.g. `vlan:23` for VLAN number 23.
            Valid VLAN tags must be in the range of 1 to 4095 inclusive.
        :type networks: list of unicodes
        :param not_networks: List of networks (defined in MAAS) to which the
            node must not be attached.  The returned noded won't be attached to
            any of the specified networks.  A network can be identified by the
            name assigned to it in MAAS; or by an `ip:` prefix followed by any
            IP address that falls within the network; or a `vlan:` prefix
            followed by a numeric VLAN tag, e.g. `vlan:23` for VLAN number 23.
            Valid VLAN tags must be in the range of 1 to 4095 inclusive.
        :type not_networks: list of unicodes
        :param not_connected_to: List of routers' MAC Addresses the returned
            node must not be connected to.
        :type connected_to: list of unicodes
        :param zone: An optional name for a physical zone the acquired
            node should be located in.
        :type zone: unicode
        :type not_in_zone: Optional list of physical zones from which the
            node should not be acquired.
        :type not_in_zone: List of unicodes.
        :param agent_name: An optional agent name to attach to the
            acquired node.
        :type agent_name: unicode

        Returns 409 if a suitable node matching the constraints could not be
        found.
        """
        form = AcquireNodeForm(data=request.data)
        maaslog.info(
            "Request from user %s to acquire a node with constraints %s",
            request.user.username, request.data)

        if not form.is_valid():
            raise ValidationError(form.errors)

        # This lock prevents a node we've picked as available from
        # becoming unavailable before our transaction commits.
        with locks.node_acquire:
            nodes = Node.objects.get_available_nodes_for_acquisition(
                request.user)
            nodes = form.filter_nodes(nodes)
            node = get_first(nodes)
            if node is None:
                constraints = form.describe_constraints()
                if constraints == '':
                    # No constraints.  That means no nodes at all were
                    # available.
                    message = "No node available."
                else:
                    message = (
                        "No available node matches constraints: %s"
                        % constraints)
                raise NodesNotAvailable(message)
            agent_name = request.data.get('agent_name', '')
            node.acquire(
                request.user, get_oauth_token(request),
                agent_name=agent_name)
            return node

    @admin_method
    @operation(idempotent=False)
    def set_zone(self, request):
        """Assign multiple nodes to a physical zone at once.

        :param zone: Zone name.  If omitted, the zone is "none" and the nodes
            will be taken out of their physical zones.
        :param nodes: system_ids of the nodes whose zones are to be set.
           (An empty list is acceptable).

        Raises 403 if the user is not an admin.
        """
        data = {
            'action': 'set_zone',
            'zone': request.data.get('zone'),
            'system_id': get_optional_list(request.data, 'nodes'),
        }
        form = BulkNodeActionForm(request.user, data=data)
        if not form.is_valid():
            raise ValidationError(form.errors)
        form.save()

    @admin_method
    @operation(idempotent=True)
    def power_parameters(self, request):
        """Retrieve power parameters for multiple nodes.

        :param id: An optional list of system ids.  Only nodes with
            matching system ids will be returned.
        :type id: iterable

        :return: A dictionary of power parameters, keyed by node system_id.

        Raises 403 if the user is not an admin.
        """
        match_ids = get_optional_list(request.GET, 'id')

        if match_ids is None:
            nodes = Node.objects.all()
        else:
            nodes = Node.objects.filter(system_id__in=match_ids)

        return {node.system_id: node.power_parameters for node in nodes}

    @operation(idempotent=True)
    def deployment_status(self, request):
        """Retrieve deployment status for multiple nodes.

        :param nodes: Mandatory list of system IDs for nodes whose status
            you wish to check.

        Returns 400 if mandatory parameters are missing.
        Returns 403 if the user has no permission to view any of the nodes.
        """
        system_ids = set(request.GET.getlist('nodes'))
        # Check the existence of these nodes first.
        self._check_system_ids_exist(system_ids)
        # Make sure that the user has the required permission.
        nodes = Node.objects.get_nodes(
            request.user, perm=NODE_PERMISSION.VIEW, ids=system_ids)
        permitted_ids = set(node.system_id for node in nodes)
        if len(nodes) != len(system_ids):
            raise PermissionDenied(
                "You don't have the required permission to view the "
                "following node(s): %s." % (
                    ', '.join(system_ids - permitted_ids)))

        # Create a dict of system_id to status.
        response = dict()
        for node in nodes:
            response[node.system_id] = node.get_deployment_status()
        return response

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('nodes_handler', [])
