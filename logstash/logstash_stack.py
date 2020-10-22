# import modules
import os
import urllib.request
from aws_cdk import (
    core,
    aws_ec2 as ec2,
    aws_s3_assets as assets,
    aws_iam as iam,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_logs as logs,
)
from helpers.constants import constants
from helpers.functions import (
    file_updated,
    kafka_get_brokers,
    user_data_init,
    instance_add_log_permissions,
    get_log_group_arn,
)
import boto3
from botocore.exceptions import ClientError

logs_client = boto3.client("logs")

dirname = os.path.dirname(__file__)
external_ip = urllib.request.urlopen("https://ident.me").read().decode("utf8")


class LogstashStack(core.Stack):
    def __init__(
        self,
        scope: core.Construct,
        id: str,
        vpc_stack,
        logstash_ec2=True,
        logstash_fargate=True,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # get s3 bucket name
        s3client = boto3.client("s3")
        s3_bucket_list = s3client.list_buckets()
        s3_bucket_name = ""
        for bkt in s3_bucket_list["Buckets"]:
            try:
                bkt_tags = s3client.get_bucket_tagging(Bucket=bkt["Name"])["TagSet"]
                for keypairs in bkt_tags:
                    if (
                        keypairs["Key"] == "aws:cloudformation:stack-name"
                        and keypairs["Value"] == "elkk-athena"
                    ):
                        s3_bucket_name = bkt["Name"]
            except ClientError as err:
                if err.response["Error"]["Code"] in ["NoSuchTagSet", "NoSuchBucket"]:
                    pass
                else:
                    print(f"Unexpected error: {err}")

        # get elastic endpoint
        esclient = boto3.client("es")
        es_domains = esclient.list_domain_names()
        try:
            es_domain = [
                dom["DomainName"]
                for dom in es_domains["DomainNames"]
                if "elkk-" in dom["DomainName"]
            ][0]
            es_endpoint = esclient.describe_elasticsearch_domain(DomainName=es_domain)
            es_endpoint = es_endpoint["DomainStatus"]["Endpoints"]["vpc"]
        except IndexError:
            es_endpoint = ""

        # assets for logstash stack
        logstash_yml = assets.Asset(
            self, "logstash_yml", path=os.path.join(dirname, "logstash.yml")
        )
        logstash_repo = assets.Asset(
            self, "logstash_repo", path=os.path.join(dirname, "logstash.repo")
        )

        # update conf file to .asset
        # kafka brokerstring does not need reformatting
        logstash_conf_asset = file_updated(
            os.path.join(dirname, "logstash.conf"),
            {
                "$s3_bucket": s3_bucket_name,
                "$es_endpoint": es_endpoint,
                "$kafka_brokers": kafka_get_brokers(),
                "$elkk_region": os.environ["CDK_DEFAULT_REGION"],
            },
        )
        logstash_conf = assets.Asset(self, "logstash.conf", path=logstash_conf_asset,)

        # logstash security group
        logstash_security_group = ec2.SecurityGroup(
            self,
            "logstash_security_group",
            vpc=vpc_stack.get_vpc,
            description="logstash security group",
            allow_all_outbound=True,
        )
        core.Tags.of(logstash_security_group).add("project", constants["PROJECT_TAG"])
        core.Tags.of(logstash_security_group).add("Name", "logstash_sg")

        # Open port 22 for SSH
        logstash_security_group.add_ingress_rule(
            ec2.Peer.ipv4(f"{external_ip}/32"), ec2.Port.tcp(22), "from own public ip",
        )

        # get security group for kafka
        ec2client = boto3.client("ec2")
        security_groups = ec2client.describe_security_groups(
            Filters=[{"Name": "tag-value", "Values": [constants["PROJECT_TAG"],]},],
        )

        # if kafka sg does not exist ... don't add it
        try:
            kafka_sg_id = [
                sg["GroupId"]
                for sg in security_groups["SecurityGroups"]
                if "kafka security group" in sg["Description"]
            ][0]
            kafka_security_group = ec2.SecurityGroup.from_security_group_id(
                self, "kafka_security_group", security_group_id=kafka_sg_id
            )

            # let in logstash
            kafka_security_group.connections.allow_from(
                logstash_security_group, ec2.Port.all_traffic(), "from logstash",
            )
        except IndexError:
            # print("kafka_sg_id and kafka_security_group not found")
            pass

        # get security group for elastic
        try:
            elastic_sg_id = [
                sg["GroupId"]
                for sg in security_groups["SecurityGroups"]
                if "elastic security group" in sg["Description"]
            ][0]
            elastic_security_group = ec2.SecurityGroup.from_security_group_id(
                self, "elastic_security_group", security_group_id=elastic_sg_id
            )

            # let in logstash
            elastic_security_group.connections.allow_from(
                logstash_security_group, ec2.Port.all_traffic(), "from logstash",
            )
        except IndexError:
            pass

        # elastic policy
        access_elastic_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "es:ListDomainNames",
                "es:DescribeElasticsearchDomain",
                "es:ESHttpPut",
            ],
            resources=["*"],
        )

        # kafka policy
        access_kafka_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["kafka:ListClusters", "kafka:GetBootstrapBrokers",],
            resources=["*"],
        )

        # s3 policy
        access_s3_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["s3:ListBucket", "s3:PutObject"],
            resources=["*"],
        )

        # create the Logstash instance
        if logstash_ec2:
            # userdata for Logstash
            logstash_userdata = user_data_init(log_group_name="elkk/logstash/instance")
            # create the instance
            logstash_instance = ec2.Instance(
                self,
                "logstash_client",
                instance_type=ec2.InstanceType(constants["LOGSTASH_INSTANCE"]),
                machine_image=ec2.AmazonLinuxImage(
                    generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2
                ),
                vpc=vpc_stack.get_vpc,
                vpc_subnets={"subnet_type": ec2.SubnetType.PUBLIC},
                key_name=constants["KEY_PAIR"],
                security_group=logstash_security_group,
                user_data=logstash_userdata,
            )
            core.Tag.add(logstash_instance, "project", constants["PROJECT_TAG"])

            # add access to the file assets
            logstash_yml.grant_read(logstash_instance)
            logstash_repo.grant_read(logstash_instance)
            logstash_conf.grant_read(logstash_instance)

            # add permissions to instance
            logstash_instance.add_to_role_policy(statement=access_elastic_policy)
            logstash_instance.add_to_role_policy(statement=access_kafka_policy)
            logstash_instance.add_to_role_policy(statement=access_s3_policy)

            # add log permissions
            instance_add_log_permissions(logstash_instance)

            # add commands to the userdata
            logstash_userdata.add_commands(
                # get setup assets files
                f"aws s3 cp s3://{logstash_yml.s3_bucket_name}/{logstash_yml.s3_object_key} /home/ec2-user/logstash.yml",
                f"aws s3 cp s3://{logstash_repo.s3_bucket_name}/{logstash_repo.s3_object_key} /home/ec2-user/logstash.repo",
                f"aws s3 cp s3://{logstash_conf.s3_bucket_name}/{logstash_conf.s3_object_key} /home/ec2-user/logstash.conf",
                # install java
                "amazon-linux-extras install java-openjdk11 -y",
                # install git
                "yum install git -y",
                # install pip
                "yum install python-pip -y",
                # get elastic output to es
                "git clone https://github.com/awslabs/logstash-output-amazon_es.git /home/ec2-user/logstash-output-amazon_es",
                # logstash
                "rpm --import https://artifacts.elastic.co/GPG-KEY-elasticsearch",
                # move logstash repo file
                "mv -f /home/ec2-user/logstash.repo /etc/yum.repos.d/logstash.repo",
                # get to the yum
                "yum install logstash -y",
                # add user to logstash group
                "usermod -a -G logstash ec2-user",
                # move logstash.yml to final location
                "mv -f /home/ec2-user/logstash.yml /etc/logstash/logstash.yml",
                # move logstash.conf to final location
                "mv -f /home/ec2-user/logstash.conf /etc/logstash/conf.d/logstash.conf",
                # move plugin
                "mkdir /usr/share/logstash/plugins",
                "mv -f /home/ec2-user/logstash-output-amazon_es /usr/share/logstash/plugins/logstash-output-amazon_es",
                # update gemfile
                """sed -i '5igem "logstash-output-amazon_es", :path => "/usr/share/logstash/plugins/logstash-output-amazon_es"' /usr/share/logstash/Gemfile""",
                # update ownership
                "chown -R logstash:logstash /etc/logstash",
                # start logstash
                "systemctl start logstash.service",
            )
            # add the signal
            logstash_userdata.add_signal_on_exit_command(resource=logstash_instance)

            # add creation policy for instance
            logstash_instance.instance.cfn_options.creation_policy = core.CfnCreationPolicy(
                resource_signal=core.CfnResourceSignal(count=1, timeout="PT10M")
            )

        # fargate for logstash
        if logstash_fargate:
            # cloudwatch log group for containers
            logstash_logs_containers = logs.LogGroup(
                self,
                "logstash_logs_containers",
                log_group_name="elkk/logstash/container",
                removal_policy=core.RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_WEEK,
            )
            # docker image for logstash
            logstash_image_asset = ecr_assets.DockerImageAsset(
                self, "logstash_image_asset", directory=dirname  # , file="Dockerfile"
            )

            # create the fargate cluster
            logstash_cluster = ecs.Cluster(
                self, "logstash_cluster", vpc=vpc_stack.get_vpc
            )
            core.Tag.add(logstash_cluster, "project", constants["PROJECT_TAG"])

            # the task
            logstash_task = ecs.FargateTaskDefinition(
                self, "logstash_task", cpu=512, memory_limit_mib=1024,
            )

            # add container to the task
            logstash_task.add_container(
                logstash_image_asset.source_hash,
                image=ecs.ContainerImage.from_docker_image_asset(logstash_image_asset),
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="elkk", log_group=logstash_logs_containers
                ),
            )

            # add permissions to the task
            logstash_task.add_to_task_role_policy(access_s3_policy)
            logstash_task.add_to_task_role_policy(access_elastic_policy)

            # the service
            logstash_service = (
                ecs.FargateService(
                    self,
                    "logstash_service",
                    cluster=logstash_cluster,
                    task_definition=logstash_task,
                    security_group=logstash_security_group,
                    deployment_controller=ecs.DeploymentController(
                        type=ecs.DeploymentControllerType.ECS
                    ),
                )
                .auto_scale_task_count(min_capacity=3, max_capacity=10)
                .scale_on_cpu_utilization(
                    "logstash_scaling",
                    target_utilization_percent=75,
                    scale_in_cooldown=core.Duration.seconds(60),
                    scale_out_cooldown=core.Duration.seconds(60),
                )
            )
