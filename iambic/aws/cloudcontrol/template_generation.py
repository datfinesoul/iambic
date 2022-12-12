import os
import pathlib

import aiofiles

from iambic.aws.cloudcontrol.utils import list_resources
from iambic.aws.iam.policy.models import ManagedPolicyTemplate
from iambic.aws.iam.policy.utils import list_managed_policies
from iambic.aws.models import AWSAccount
from iambic.aws.utils import get_aws_account_map, normalize_boto3_resp
from iambic.config.models import Config
from iambic.core import noq_json as json
from iambic.core.logger import log
from iambic.core.template_generation import (
    base_group_str_attribute,
    get_existing_template_file_map,
    group_dict_attribute,
    group_int_or_str_attribute,
)
from iambic.core.utils import NoqSemaphore, resource_file_upsert


async def generate_cloudcontrol_resource_files(
    aws_account: AWSAccount,
) -> dict:
    # TODO: Depends on resource type
    # account_resource_dir = get_account_managed_policy_resource_dir(
    #     aws_account.account_id
    # )
    resource_file_upsert_semaphore = NoqSemaphore(resource_file_upsert, 10)
    messages = []

    response = dict(account_id=aws_account.account_id, resources=[])
    # TODO: We need to parse all active regions, not just the default one
    cloudcontrol_client = await aws_account.get_boto3_client(
        "cloudcontrol", region_name=aws_account.default_region
    )

    # TODO: Need this data first for AWS SSO:
    cloudcontrol_client.list_resources(
        TypeName=resource_type.get("type"),
        ResourceModel=json.dumps(
            {
                "InstanceArn": "arn:aws:sso:::instance/ssoins-7223e919c6a1baec",
                "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-7223e919c6a1baec/ps-1f0281f0000336b3",
            }
        ),
    )
    supported_resource_types = [
        {
            "type": "AWS::SSO::PermissionSet",
            "ResourceModel": json.dumps(["InstanceArn", "PermissionSetArn"]),
        },
        {
            "type": "AWS::SSO::InstanceAccessControlAttributeConfiguration",
        },
        {
            "type": "AWS::SSO::Assignment",
        },
    ]

    account_resources = await list_resources(
        cloudcontrol_client, supported_resource_types
    )

    log.info(
        "Retrieved Supported AWS CloudControl resources.",
        account_id=aws_account.account_id,
        account_name=aws_account.account_name,
        managed_policy_count=len(account_resources),
    )

    for managed_policy in account_resources:
        policy_path = os.path.join(
            account_resource_dir, f'{managed_policy["PolicyName"]}.json'
        )
        response["managed_policies"].append(
            {
                "file_path": policy_path,
                "policy_name": managed_policy["PolicyName"],
                "arn": managed_policy["Arn"],
                "account_id": aws_account.account_id,
            }
        )
        messages.append(
            dict(
                file_path=policy_path, content_as_dict=managed_policy, replace_file=True
            )
        )

    await resource_file_upsert_semaphore.process(messages)
    log.info(
        "Finished caching AWS IAM Managed Policies.",
        account_id=aws_account.account_id,
        managed_policy_count=len(account_managed_policies),
    )

    return response


async def generate_cloudcontrol_templates(configs: list[Config], base_output_dir: str):
    aws_account_map = await get_aws_account_map(configs)
    # existing_template_map = await get_existing_template_file_map(
    #     base_output_dir, "NOQ::"
    # )
    generate_cloudcontrol_resource_files_semaphore = NoqSemaphore(
        generate_cloudcontrol_resource_files, 25
    )

    log.info("Generating AWS Cloud Control templates.")
    log.info(
        "Beginning to retrieve AWS Cloud Control templates.",
        accounts=list(aws_account_map.keys()),
    )

    account_cloudcontrol_resources = (
        await generate_cloudcontrol_resource_files_semaphore.process(
            [{"aws_account": aws_account} for aws_account in aws_account_map.values()]
        )
    )
    messages = []
    for account in account_cloudcontrol_resources:
        for managed_policy in account["managed_policies"]:
            messages.append(
                {
                    "policy_name": managed_policy["policy_name"],
                    "arn": managed_policy["arn"],
                    "file_path": managed_policy["file_path"],
                    "aws_account": aws_account_map[managed_policy["account_id"]],
                }
            )

    log.info("Finished retrieving managed policy details")

    # Use these for testing `create_templated_managed_policy`
    # account_managed_policy_output = json.dumps(account_managed_policies)
    # with open("account_managed_policy_output.json", "w") as f:
    #     f.write(account_managed_policy_output)
    # with open("account_managed_policy_output.json") as f:
    #     account_managed_policies = json.loads(f.read())

    log.info("Grouping managed policies")
    # Move everything to required structure
    for account_mp_elem in range(len(account_managed_policies)):
        for mp_elem in range(
            len(account_managed_policies[account_mp_elem]["managed_policies"])
        ):
            policy_name = account_managed_policies[account_mp_elem]["managed_policies"][
                mp_elem
            ].pop("policy_name")
            account_managed_policies[account_mp_elem]["managed_policies"][mp_elem][
                "resource_val"
            ] = policy_name

        account_managed_policies[account_mp_elem][
            "resources"
        ] = account_managed_policies[account_mp_elem].pop("managed_policies", [])

    grouped_managed_policy_map = await base_group_str_attribute(
        aws_account_map, account_managed_policies
    )

    log.info("Writing templated roles")
    for policy_name, policy_refs in grouped_managed_policy_map.items():
        await create_templated_managed_policy(
            aws_account_map,
            policy_name,
            policy_refs,
            resource_dir,
            existing_template_map,
        )

    log.info("Finished templated managed policy generation")
