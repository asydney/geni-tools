#----------------------------------------------------------------------         
# Copyright (c) 2010-2014 Raytheon BBN Technologies
#                                                                               
# Permission is hereby granted, free of charge, to any person obtaining         
# a copy of this software and/or hardware specification (the "Work") to         
# deal in the Work without restriction, including without limitation the        
# rights to use, copy, modify, merge, publish, distribute, sublicense,          
# and/or sell copies of the Work, and to permit persons to whom the Work        
# is furnished to do so, subject to the following conditions:                   
#                                                                               
# The above copyright notice and this permission notice shall be                
# included in all copies or substantial portions of the Work.                   
#                                                                               
# THE WORK IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS           
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF                    
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND                         
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT                   
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,                  
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,            
# OUT OF OR IN CONNECTION WITH THE WORK OR THE USE OR OTHER DEALINGS            
# IN THE WORK.                                                                  
#----------------------------------------------------------------------         
" Base class for authorizers of AM calls"

import gcf.sfa.trust.gid as gid

# Name of all AM Methods
class AM_Methods:
    # AM API V2 Methods
    LIST_RESOURCES_V2 = 'ListResources_V2'
    LIST_RESOURCES_FOR_SLICE_V2 = 'ListResourcesForSlice_V2'
    CREATE_SLIVER_V2 = "CreateSliver_V2"
    DELETE_SLIVER_V2 = "DeleteSliver_V2"
    RENEW_SLIVER_V2 = "RenewSliver_V2"
    SLIVER_STATUS_V2 = "SliverStatus_V2"
    SHUTDOWN_V2 = "Shutdown_V2"

    # AM API V3 Methods
    LIST_RESOURCES_V3 = 'ListResources_V3'
    ALLOCATE_V3 = "Allocate_V3"
    PROVISION_V3 = "Provision_V3"
    DELETE_V3 = "Delete_V3"
    PERFORM_OPERATIONAL_ACTION_V3 = "PerformOperationalAction_V3"
    STATUS_V3 = "Status_V3"
    DESCRIBE_V3 = "Describe_V3"
    RENEW_V3 = "Renew_V3"
    SHUTDOWN_V3 = "Shutdown_V3"

class Base_Authorizer:
    def __init__(self, root_cert):
        self._root_cert = root_cert
        self._logger = None

    # Try to authorize the call. 
    # Success is silent.
    # Failure raise an exception indicating an authorization error 
    #
    # Arguments:
    #   method : name of AM API method
    #   caller : GID (cert) of caller
    #   creds : List of credential/type pairs
    #   args : Dictionary of name/value pairs of AM call arguments
    #   opts : Dictionary of user provided options
    def authorize(self, method, caller, creds, args, opts):
        if self._logger:
            caller_urn = gid.GID(string=caller).get_urn()
            self._logger.info("Authorizing %s %s #Creds = %s Args = %s Opts =%s" % \
                                  (method, caller_urn, len(creds), \
                                       args.keys(), opts.keys()))
        

