#!/usr/bin/env python

import os
import pprint
import json
import time
import logging

from parsl.execution_provider.execution_provider_base import ExecutionProvider
import parsl.execution_provider.aws.template
import parsl.execution_provider.error as ep_error

from string import Template

AWS_REGIONS = ['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2']

DEFAULT_REGION = 'us-east-2'

translate_table = { 'PD': 'PENDING',
                    'R': 'RUNNING',
                    'CA': 'CANCELLED',
                    'CF': 'PENDING',   # (configuring),
                    'CG': 'RUNNING',   # (completing),
                    'CD': 'COMPLETED',
                    'F': 'FAILED',     # (failed),
                    'TO': 'TIMEOUT',   # (timeout),
                    'NF': 'FAILED',    # (node failure),
                    'RV': 'FAILED',    # (revoked) and
                    'SE': 'FAILED'}    # (special exit state


class EC2Provider(ExecutionProvider):

    def __init__(self, config):
        """Initialize provider"""
        self.config = self.read_configs(config)
        self.set_instance_vars()
        self.logger = logging.getLogger(__name__)

        if not os.path.exists(
            self.config["execution"]["options"]["submit_script_dir"]):
            os.makedirs(self.config["execution"]
                        ["options"]["submit_script_dir"])

        try:
            self.read_state_file(config['recover_from_state_file'])

        except Exception as e:
            self.create_vpc().id
            self.logger.info("No State File. Cannot load previous options. Creating new infrastructure")
            self.write_state_file()

    def set_instance_vars(self):
        """Initialize instance variables"""
        self.current_blocksize = 0
        self.sitename = self.config['site']
        self.session = self.create_session(self.config)
        self.client = self.session.client('ec2')
        self.ec2 = self.session.resource('ec2')
        self.instances = []
        self.instance_states = {}
        self.vpc_id = 0
        self.sg_id = 0
        self.sn_ids = []

    def read_state_file(self, state_file_path):
        """If this script has been run previously, it will be persisitent
        by writing resource ids to state file. On run, the script looks for a state file
        before creating new infrastructure"""
        try:
            state = None
            with open(state_file_path, 'r') as fh:
                state = json.load(fh)
            self.vpc_id = state['vpcID']
            self.sg_id = state['sgID']
            self.sn_ids = state['snIDs']
            self.instances = state['instances']
        except Exception as e:
            raise e

    def write_state_file(self, state_file_path):
        state = {'vpcID' : self.vpc_id,
                 'sgID' : self.sg_id,
                 'snIDs' : self.sn_ids,
                 'instances' : self.instances,
                 "instanceState" : self.instance_states }

        with open(state_file_path, 'w') as fh :
            fh.write(json.dumps(state, indent=4))

    def _read_conf(self, config_file):
        """read config file"""
        config = json.load(open(config_file, 'r'))
        return config

    def pretty_configs(self, configs):
        """prettyprint config"""
        printer = pprint.PrettyPrinter(indent=4)
        printer.pprint(configs)

    def read_configs(self, config_file):
        """Read config file"""
        config = self._read_conf(config_file)
        return config

    def create_session(self, config={}):
        """Create boto3 session.
        If config contains ec2credentialsfile, it will use that file, if not,
        it will check for a ~/.aws/credentials file and use that.
        If not found, it will look for environment variables containing aws auth
        information. If none of those options work, it will let boto attempt to
        figure out who you are. if that fails, we cannot proceed"""
        if 'ec2credentialsfile' in config:
            config['ec2credentialsfile'] = os.path.expanduser(
                config['ec2credentialsfile'])
            config['ec2credentialsfile'] = os.path.expandvars(
                config['ec2credentialsfile'])

            cred_lines = open(config['ec2credentialsfile']).readlines()
            cred_details = cred_lines[1].split(',')
            credentials = {'AWS_Username': cred_lines[0],
                           'AWSAccessKeyId': cred_lines[1].split(' = ')[1],
                           'AWSSecretKey': cred_lines[2].split(' = ')[1]}
            config.update(credentials)
            session = boto3.session.Session(aws_access_key_id=credentials['AWSAccessKeyId'],
                                            aws_secret_access_key=credentials['AWSSecretKey'],)
            return session
        elif os.path.isfile(os.path.expanduser('~/.aws/credentials')):
            cred_lines = open(os.path.expanduser(
                '~/.aws/credentials')).readlines()
            credentials = {'AWS_Username': cred_lines[0],
                           'AWSAccessKeyId': cred_lines[1].split(' = ')[1],
                           'AWSSecretKey': cred_lines[2].split(' = ')[1]}
            config.update(credentials)
            session = boto3.session.Session()
            return session
        elif (os.getenv("AWS_ACCESS_KEY_ID") is not None
              and os.getenv("AWS_SECRET_ACCESS_KEY") is not None):
            session = boto3.session.Session(aws_access_key_id=credentials['AWSAccessKeyId'],
                                            aws_secret_access_key=credentials['AWSSecretKey'],)
            return session
        else:
            try:
                session = boto3.session.Session()
                return session
            except Exception as e:
                self.logger.error("Credentials not found. Cannot Continue")
                exit(-1)

    def create_vpc(self):
        """Create and configure VPC"""
        try:
            vpc = self.ec2.create_vpc(
                CidrBlock='172.32.0.0/16',
                AmazonProvidedIpv6CidrBlock=False,
            )
        except Exception as e:
            self.logger.error("{}\n".format(e))
        internet_gateway = self.ec2.create_internet_gateway()
        internet_gateway.attach_to_vpc(VpcId=vpc.vpc_id)  # Returns None
        self.internet_gateway = internet_gateway.id
        route_table = self.config_route_table(vpc, internet_gateway)
        self.route_table = route_table.id
        availability_zones = self.client.describe_availability_zones()
        for num, zone in enumerate(availability_zones['AvailabilityZones']):
            if zone['State'] == "available":
                subnet = vpc.create_subnet(
                    CidrBlock='172.32.{}.0/20'.format(16 * num),
                    AvailabilityZone=zone['ZoneName'])
                route_table.associate_with_subnet(SubnetId=subnet.id)
                self.sn_ids.append(subnet.id)
            else:
                print("{} unavailable".format(zone['ZoneName']))
        # Security groups
        sg = self.security_group(vpc)
        self.vpc_id = vpc.id
        self.write_state_file()
        return vpc

    def security_group(self, vpc):
        """Create and configure security group.
        Allows all ICMP in, all tcp and udp in within vpc
        """
        sg = vpc.create_security_group(
            GroupName="private-subnet",
            Description="security group for remote executors")

        ip_ranges = [{
            'CidrIp': '172.32.0.0/16'
        }]

        # Allows all ICMP in, all tcp and udp in within vpc

        inPerms = [{
            'IpProtocol': 'TCP',
            'FromPort': 0,
            'ToPort': 65535,
            'IpRanges': ip_ranges,
        }, {
            'IpProtocol': 'UDP',
            'FromPort': 0,
            'ToPort': 65535,
            'IpRanges': ip_ranges,
        }, {
            'IpProtocol': 'ICMP',
            'FromPort': -1,
            'ToPort': -1,
            'IpRanges': [{'CidrIp': '0.0.0.0/0'}],
        }]
        # Allows all TCP out, all tcp and udp out within vpc
        outPerms = [{
            'IpProtocol': 'TCP',
            'FromPort': 0,
            'ToPort': 65535,
            'IpRanges': [{'CidrIp': '0.0.0.0/0'}],
        }, {
            'IpProtocol': 'TCP',
            'FromPort': 0,
            'ToPort': 65535,
            'IpRanges': ip_ranges,
        }, {
            'IpProtocol': 'UDP',
            'FromPort': 0,
            'ToPort': 65535,
            'IpRanges': ip_ranges,
        }, ]

        sg.authorize_ingress(IpPermissions=inPerms)
        sg.authorize_egress(IpPermissions=outPerms)
        self.sg_id = sg.id
        return sg

    def config_route_table(self, vpc, internet_gateway):
        """Configure route table for vpc"""
        route_table = vpc.create_route_table()
        route_ig_ipv4 = route_table.create_route(
            DestinationCidrBlock='0.0.0.0/0',
            GatewayId=internet_gateway.internet_gateway_id)
        return route_table

    def scale_out(self, size):
        """Scale cluster out (larger)"""
        for i in range(size * self.config['nodeGranularity']):
            self.spin_up_instance()
        self.current_blocksize += size * self.config['nodeGranularity']

    def scale_in(self, size):
        """Scale cluster in (smaller)"""
        for i in range(size * self.config['nodeGranularity']):
            self.shut_down_instance()
        self.current_blocksize -= size*self.config['nodeGranularity']

    def spin_up_instance(self):
        """Starts an instance in the vpc in first available
        subnet. Starts up n instances at a time where n is
        node granularity from config"""
        instance_type = self.config['instancetype']
        subnet = self.sn_ids[0]
        ami_id = self.config['AMIID']
        total_instances = len(self.instances) + self.config['nodeGranularity']
        if total_instances > self.config['maxNodes']:
            warning = "You have requested more instances ({}) than your maxNodes ({}). Cannot Continue\n".format(
                total_instances,
                self.config['maxNodes'])
            self.logger.warn(warning)
            return -1
        instance = self.ec2.create_instances(
            InstanceType=instance_type,
            ImageId=ami_id,
            MinCount=1,
            MaxCount=self.config['nodeGranularity'],
            KeyName=self.config['AWSKeyName'],
            SubnetId=subnet,
            SecurityGroupIds=[self.sg_id],
            UserData=template.template_string)
        self.instances.append(instance[0].id)
        self.logger.info(
            "Started up 1 instance. Instance type:{}".format(instance_type))
        self.write_state_file()
        return instance

    def shut_down_instance(self, instances=None):
        """Shuts down a list of instances if provided or the last
        instance started up if none provided"""
        if instances and len(self.instances > 0):
            term = self.client.terminate_instances(InstanceIds=instances)
            self.logger.info(
                "Shut down {} instances (ids:{}".format(
                    len(instances), str(instances)))
        elif len(self.instances) > 0:
            instance = self.instances.pop()
            term = self.client.terminate_instances(InstanceIds=[instance])
            self.logger.info("Shut down 1 instance (id:{})".format(instance))
        else:
            self.logger.warn("No Instances to shut down.\n")
            return -1
        self.get_instance_state()
        self.write_state_file()
        return term

    def get_instance_state(self, instances=None):
        """Get stateus of all instances on EC2 which were started by this
        file"""
        if instances:
            desc = self.client.describe_instances(InstanceIds=instances)
        else:
            desc = self.client.describe_instances(InstanceIds=self.instances)
        # pprint.pprint(desc['Reservations'],indent=4)
        for i in range(len(desc['Reservations'])):
            instance = desc['Reservations'][i]['Instances'][0]
            self.instance_states[instance['InstanceId']
                                 ] = instance['State']['Name']
        self.write_state_file()
        return self.instance_states

    ###########################################################################################################
    # Status
    ###########################################################################################################
    def _status(self):
        ''' Internal: Do not call. Returns the status list for a list of job_ids
        Args:
        self.
        job_ids : [<job_id> ...]
        Returns :
        [status...]
        '''

        job_id_list  = ','.join(self.resources.keys())

        jobs_missing = list(self.resources.keys())

        retcode, stdout, stderr = execute_wait("squeue --job {0}".format(job_id_list), 3)
        for line in stdout.split('\n'):
            parts = line.split()
            if parts and parts[0] != 'JOBID' :
                job_id = parts[0]
                status = translate_table.get(parts[4], 'UNKNOWN')
                self.resources[job_id]['status'] = status
                jobs_missing.remove(job_id)

        # squeue does not report on jobs that are not running. So we are filling in the
        # blanks for missing jobs, we might lose some information about why the jobs failed.
        for missing_job in jobs_missing:
            if self.resources[missing_job]['status'] in ['PENDING', 'RUNNING']:
                self.resources[missing_job]['status'] = translate_table['CD']

    def status (self, job_ids):
        '''  Get the status of a list of jobs identified by their ids.
        Args:
            - job_ids (List of ids) : List of identifiers for the jobs
        Returns:
            - List of status codes.
        '''
        self._status()
        return [self.resources[jid]['status'] for jid in job_ids]

    ###########################################################################################################
    # Submit
    ###########################################################################################################
    def submit (self, cmd_string, blocksize, job_name="parsl.auto"):
        ''' Submits the cmd_string onto an Local Resource Manager job of blocksize parallel elements.
        Submit returns an ID that corresponds to the task that was just submitted.
        If tasks_per_node <  1:
             1/tasks_per_node is provisioned
        If tasks_per_node == 1:
             A single node is provisioned
        If tasks_per_node >  1 :
             tasks_per_node * blocksize number of nodes are provisioned.
        Args:
             - cmd_string  :(String) Commandline invocation to be made on the remote side.
             - blocksize   :(float)
        Kwargs:
             - job_name (String): Name for job, must be unique
        Returns:
             - None: At capacity, cannot provision more
             - job_id: (string) Identifier for the job
        '''

        if self.current_blocksize >= self.config["execution"]["options"]["max_parallelism"]:
            logger.warn("[%s] at capacity, cannot add more blocks now", self.sitename)
            return None

        job_name = "parsl.{0}.{1}".format(job_name,time.time())

        script_path = "{0}/{1}.submit".format(self.config["execution"]["options"]["submit_script_dir"],
                                              job_name)

        nodes = math.ceil(float(blocksize) / self.config["execution"]["options"]["tasks_per_node"])
        logger.debug("Requesting blocksize:%s tasks_per_node:%s nodes:%s", blocksize,
                     self.config["execution"]["options"]["tasks_per_node"],nodes)

        job_config = self.config["execution"]["options"]
        job_config["nodes"] = nodes
        job_config["slurm_overrides"] = job_config.get("slurm_overrides", '')
        job_config["user_script"] = cmd_string

        retcode, stdout, stderr = execute_wait("sbatch {0}".format(script_path), 3)
        logger.debug ("Retcode:%s STDOUT:%s STDERR:%s", retcode,
                      stdout.strip(), stderr.strip())

        job_id = None

        if retcode == 0 :
            for line in stdout.split('\n'):
                if line.startswith("Submitted batch job"):
                    job_id = line.split("Submitted batch job")[1].strip()
                    self.resources[job_id] = {'job_id' : job_id,
                                              'status' : 'PENDING',
                                              'blocksize'   : blocksize }
        else:
            print("Submission of command to scale_out failed")

        return job_id

    ###########################################################################################################
    # Cancel
    ###########################################################################################################
    def cancel(self, job_ids):
        ''' Cancels the jobs specified by a list of job ids
        Args:
        job_ids : [<job_id> ...]
        Returns :
        [True/False...] : If the cancel operation fails the entire list will be False.
        '''

        job_id_list = ' '.join(job_ids)
        retcode, stdout, stderr = execute_wait("scancel {0}".format(job_id_list), 3)
        rets = None
        if retcode == 0 :
            for jid in job_ids:
                self.resources[jid]['status'] = translate_table['CA'] # Setting state to cancelled
            rets = [True for i in job_ids]
        else:
            rets = [False for i in job_ids]

        return rets

    def show_summary(self):
        """Print human readable summary of current
        AWS state to log and to console"""
        self.get_instance_state()
        status_string = "EC2 Summary:\n\tVPC IDs: {}\n\tSubnet IDs: \
{}\n\tSecurity Group ID: {}\n\tRunning Instance IDs: {}\n".format(
            self.vpc_id,
            self.sn_ids,
            self.sg_id,
            self.instances)
        status_string += "\tInstance States:\n\t\t"
        self.get_instance_state()
        for state in self.instance_states.keys():
            status_string += "Instance ID: {}  State: {}\n\t\t".format(
                state, self.instance_states[state])
        status_string += "\n"
        self.write_state_file()
        self.logger.info(status_string)
        return status_string

    def teardown(self):
        """Terminate all EC2 instances, delete all subnets,
        delete security group, delete vpc
        and reset all instance variables
        """
        self.shut_down_instance(self.instances)
        self.instances = []
        try:
            self.client.delete_internet_gateway(
                InternetGatewayId=self.internet_gateway)
            self.internet_gateway = None
            self.client.delete_route_table(RouteTableId=self.route_table)
            self.route_table = None
            for subnet in list(self.sn_ids):
                # Cast to list ensures that this is a copy
                # Which is important because it means that
                # the length of the list won't change during iteration
                self.client.delete_subnet(SubnetId=subnet)
                self.sn_ids.remove(subnet)
            self.client.delete_security_group(GroupId=self.sg_id)
            self.sg_id = None
            self.client.delete_vpc(VpcId=self.vpc_id)
            self.vpc_id = None
        except Exception as e:
            self.logger.error(
                "{}".format(e))
            raise e
        self.show_summary()
        self.write_state_file()
        os.remove('awsproviderstate.json')


if __name__ == '__main__':
    conf = "providerconf.json"
    provider = EC2Provider(conf)
    # provider.scale_out(1)
    print(provider.show_summary())
    # provider.scale_in(1)
    provider.status()
    provider.teardown()
    print(provider.show_summary())